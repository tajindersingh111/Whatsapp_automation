"""End-to-end flows: Meta webhook -> queue -> agent -> WhatsApp, store webhooks, COD confirmation."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from app.models import Contact, Conversation, Job, Message, Order, Product
from tests.conftest import (
    CUSTOMER,
    FakeLLM,
    drain,
    meta_payload,
    msg,
    post_meta,
    post_store_event,
    text_block,
    tool_block,
)


async def sync_catalog(api, tenant_id):
    from tests.conftest import ADMIN

    r = await api.post(f"/admin/tenants/{tenant_id}/store/sync", headers=ADMIN)
    assert r.status_code == 200
    await drain()


async def test_meta_webhook_rejects_bad_signature(api, tenant):
    r = await api.post("/webhooks/meta", json=meta_payload("hi", wamid="w1"), headers={"X-Hub-Signature-256": "sha256=bad"})
    assert r.status_code == 401


async def test_meta_verification_handshake(api):
    ok = await api.get("/webhooks/meta", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "42"})
    assert ok.status_code == 200 and ok.text == "42"
    bad = await api.get("/webhooks/meta", params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "42"})
    assert bad.status_code == 403


async def test_catalog_sync_and_search(api, tenant, db):
    await sync_catalog(api, tenant.id)
    count = len((await db.scalars(select(Product).where(Product.tenant_id == tenant.id))).all())
    assert count == 4
    from app.services.catalog import search_products

    blue = await search_products(db, tenant.id, "blue bedsheet king size chahiye")
    assert blue[0].external_id == "p1"
    cheap = await search_products(db, tenant.id, "bedsheet", max_price=1000)
    assert [p.external_id for p in cheap] == ["p2"]


async def test_inbound_message_gets_ai_reply_with_product_search(api, tenant, db, graph, fake_llm: FakeLLM):
    await sync_catalog(api, tenant.id)
    fake_llm.script = [
        msg(tool_block("search_products", {"query": "blue bedsheet"}), stop_reason="tool_use"),
        msg(tool_block("update_lead", {"stage": "interested", "note": "wants blue king bedsheet"}), stop_reason="tool_use"),
        msg(text_block("Ye dekhiye: *Indigo Dabu Bedsheet* ₹1,899 (MRP ₹2,499). Size king hai. Order karna chahenge?")),
    ]
    r = await post_meta(api, meta_payload("Blue bedsheet hai kya?", wamid="wamid.in.1"))
    assert r.status_code == 200
    await drain()

    # Customer saw exactly one reply.
    texts = [s for s in graph.sent if s["type"] == "text"]
    assert len(texts) == 1 and "Indigo" in texts[0]["text"]["body"]
    assert texts[0]["to"] == CUSTOMER
    assert graph.read == ["wamid.in.1"]

    # The tool really searched the synced catalog.
    result = fake_llm.tool_results(1)[0]
    assert '"product_id": "p1"' in result["content"]

    # The brand brain is in the system prompt, cached; per-turn context is after the breakpoint.
    system = fake_llm.calls[0]["system"]
    assert "Jaipur Linen Co" in system[1]["text"] and "FIRST10" in system[1]["text"]
    assert system[1]["cache_control"] == {"type": "ephemeral"}
    assert "+919876543210" in system[2]["text"]

    contact = await db.scalar(select(Contact).where(Contact.wa_id == CUSTOMER))
    assert contact.lead_stage == "interested" and contact.name == "Priya"
    out = (await db.scalars(select(Message).where(Message.direction == "out"))).all()
    assert [m.status for m in out] == ["sent"]


async def test_duplicate_and_burst_messages_get_one_reply(api, tenant, db, graph, fake_llm: FakeLLM):
    fake_llm.script = [msg(text_block("Ji, bataiye kya dekh rahe hain?"))]
    await post_meta(api, meta_payload("hello", wamid="wamid.a"))
    await post_meta(api, meta_payload("hello", wamid="wamid.a"))  # Meta retry
    await post_meta(api, meta_payload("bedsheet chahiye", wamid="wamid.b"))  # second message before we answered
    await drain()
    inbound = (await db.scalars(select(Message).where(Message.direction == "in"))).all()
    assert [m.wa_message_id for m in inbound] == ["wamid.a", "wamid.b"]
    assert len([s for s in graph.sent if s["type"] == "text"]) == 1
    # Both customer messages reached the model as one user turn.
    assert fake_llm.calls[0]["messages"][-1] == {"role": "user", "content": "hello\nbedsheet chahiye"}


async def test_bot_places_cod_order_only_after_confirmation(api, tenant, db, graph, mock_store, fake_llm: FakeLLM):
    await sync_catalog(api, tenant.id)
    address = {"name": "Priya Sharma", "address_line1": "12 MI Road", "city": "Jaipur", "state": "Rajasthan", "pincode": "302001"}
    fake_llm.script = [
        msg(tool_block("place_cod_order", {"customer": address, "items": [{"product_id": "p1", "quantity": 1}], "customer_confirmed": False}), stop_reason="tool_use"),
        msg(tool_block("place_cod_order", {"customer": {**address, "pincode": "30200"}, "items": [{"product_id": "p1", "quantity": 1}], "customer_confirmed": True}), stop_reason="tool_use"),
        msg(tool_block("place_cod_order", {"customer": address, "items": [{"product_id": "p3", "quantity": 1}], "customer_confirmed": True}), stop_reason="tool_use"),
        msg(tool_block("place_cod_order", {"customer": address, "items": [{"product_id": "p1", "quantity": 2}], "customer_confirmed": True}), stop_reason="tool_use"),
        msg(text_block("Order placed! Number 1001, total ₹3,798, Cash on Delivery.")),
    ]
    await post_meta(api, meta_payload("haan confirm karo", wamid="wamid.c"))
    await drain()

    errors = [fake_llm.tool_results(i)[0] for i in (1, 2, 3)]
    assert all(e["is_error"] for e in errors)
    assert "confirm" in errors[0]["content"]
    assert "Pincode" in errors[1]["content"]
    assert "variant_id" in errors[2]["content"]  # p3 has variants
    placed = fake_llm.tool_results(4)[0]
    assert not placed["is_error"] and '"order_number": "1001"' in placed["content"]

    store_order = mock_store.state.orders["ord_1001"]
    assert store_order["payment_method"] == "cod" and store_order["total"] == 3798
    assert store_order["customer"]["phone"] == CUSTOMER
    order = await db.scalar(select(Order).where(Order.external_id == "ord_1001"))
    assert order.created_via == "bot" and order.cod_status == "confirmed"
    contact = await db.scalar(select(Contact).where(Contact.wa_id == CUSTOMER))
    assert contact.lead_stage == "ordered"


async def test_store_cod_order_triggers_confirmation_template_and_button_reply(api, tenant, db, graph, mock_store, fake_llm):
    # A COD order placed on the website.
    store_order = {
        "id": "ord_77", "number": "77", "status": "pending", "payment_method": "cod", "total": 1899,
        "customer": {"name": "Rahul Verma", "phone": "+91 98765 43210"},
        "items": [{"title": "Indigo Bedsheet", "quantity": 1, "price": 1899}],
    }
    mock_store.state.orders["ord_77"] = dict(store_order)
    r = await post_store_event(api, tenant.id, {"event_id": "evt-1", "type": "order.created", "order": store_order})
    assert r.status_code == 200
    r = await post_store_event(api, tenant.id, {"event_id": "evt-1", "type": "order.created", "order": store_order})  # retry
    await drain()

    templates = [s for s in graph.sent if s["type"] == "template"]
    assert len(templates) == 1
    tpl = templates[0]["template"]
    assert tpl["name"] == "cod_order_confirmation" and templates[0]["to"] == CUSTOMER
    assert [p["text"] for p in tpl["components"][0]["parameters"]] == ["Rahul", "77", "₹1,899"]
    order = await db.scalar(select(Order).where(Order.external_id == "ord_77"))
    await db.refresh(order)
    assert order.cod_status == "pending"
    confirm_payload = tpl["components"][1]["parameters"][0]["payload"]
    assert confirm_payload == f"cod:confirm:{order.id}"

    # A reminder is scheduled for later, not sent now.
    reminder = await db.scalar(select(Job).where(Job.kind == "cod_reminder"))
    assert reminder.status == "queued" and reminder.run_at > datetime.now(timezone.utc) + timedelta(hours=3)

    # Customer taps "Confirm" on the template.
    await post_meta(api, meta_payload(wamid="wamid.btn", button=(confirm_payload, "Confirm order"), name="Rahul"))
    await drain()
    await db.refresh(order)
    assert order.cod_status == "confirmed"
    assert mock_store.state.orders["ord_77"]["status"] == "confirmed"
    assert ("POST", "/api/wa-agent/orders/ord_77/confirm-cod") in mock_store.state.calls
    last = graph.sent[-1]
    assert last["type"] == "text" and "77" in last["text"]["body"] and "confirmed" in last["text"]["body"]
    assert fake_llm.calls == []  # button handled by the flow, no AI call

    # The due reminder now does nothing because the order is confirmed.
    sent_before = len(graph.sent)
    await db.execute(update(Job).where(Job.kind == "cod_reminder").values(run_at=datetime.now(timezone.utc)))
    await db.commit()
    await drain()
    assert len(graph.sent) == sent_before


async def test_unanswered_cod_order_becomes_no_response(api, tenant, db, graph):
    order = {"id": "ord_5", "number": "5", "status": "pending", "payment_method": "cod", "total": 899, "customer": {"name": "A", "phone": "9123456780"}}
    await post_store_event(api, tenant.id, {"event_id": "e5", "type": "order.created", "order": order})
    await drain()
    for _ in range(2):  # reminder 1 sends again, reminder 2 gives up
        await db.execute(update(Job).where(Job.kind == "cod_reminder", Job.status == "queued").values(run_at=datetime.now(timezone.utc)))
        await db.commit()
        await drain()
    row = await db.scalar(select(Order).where(Order.external_id == "ord_5"))
    await db.refresh(row)
    assert row.cod_status == "no_response" and row.cod_reminders_sent == 1
    assert len([s for s in graph.sent if s["type"] == "template"]) == 2


async def test_shipping_update_sent_once(api, tenant, db, graph):
    order = {
        "id": "ord_9", "number": "9", "status": "shipped", "payment_method": "prepaid", "total": 899,
        "customer": {"name": "Neha", "phone": "919811112222"},
        "tracking": {"courier": "Delhivery", "awb": "AWB9", "url": "https://track.example.com/9"},
    }
    await post_store_event(api, tenant.id, {"event_id": "s1", "type": "order.updated", "order": order})
    await post_store_event(api, tenant.id, {"event_id": "s2", "type": "order.updated", "order": order})
    await drain()
    ship = [s for s in graph.sent if s["type"] == "template"]
    assert len(ship) == 1 and ship[0]["template"]["name"] == "order_shipped"
    assert ship[0]["template"]["components"][0]["parameters"][3]["text"] == "https://track.example.com/9"


async def test_track_order_does_not_leak_other_customers(api, tenant, db, graph, mock_store, fake_llm: FakeLLM):
    mock_store.state.orders["ord_1"] = {
        "id": "ord_1", "number": "1", "status": "shipped", "payment_method": "cod", "total": 500,
        "customer": {"name": "Someone Else", "phone": "919999999999"}, "items": [], "created_at": "2026-10-01T00:00:00Z",
    }
    fake_llm.script = [
        msg(tool_block("track_order", {"order_number": "1"}), stop_reason="tool_use"),
        msg(text_block("Ye order aapke number par registered nahi hai.")),
    ]
    await post_meta(api, meta_payload("mera order 1 kahan hai", wamid="wamid.t"))
    await drain()
    result = fake_llm.tool_results(1)[0]["content"]
    assert "Someone Else" not in result and "different phone number" in result


async def test_handoff_pauses_bot(api, tenant, db, graph, fake_llm: FakeLLM):
    fake_llm.script = [
        msg(tool_block("handoff_to_human", {"reason": "damaged product complaint"}), stop_reason="tool_use"),
        msg(text_block("Sorry! Hamari team aapse jaldi baat karegi.")),
    ]
    await post_meta(api, meta_payload("product toota hua aaya", wamid="wamid.h1"))
    await drain()
    conv = await db.scalar(select(Conversation))
    await db.refresh(conv)
    assert conv.mode == "human" and "damaged" in conv.handoff_reason
    await post_meta(api, meta_payload("hello??", wamid="wamid.h2"))
    await drain()
    assert len(fake_llm.calls) == 2  # no new AI call while a human owns the chat


async def test_opt_out_stops_bot(api, tenant, db, graph, fake_llm):
    await post_meta(api, meta_payload("STOP", wamid="wamid.s"))
    await drain()
    contact = await db.scalar(select(Contact).where(Contact.wa_id == CUSTOMER))
    assert contact.opted_out
    assert "unsubscribed" in graph.sent[-1]["text"]["body"]
    assert fake_llm.calls == []


async def test_lead_followup_after_silence(api, tenant, db, graph, fake_llm: FakeLLM):
    fake_llm.script = [
        msg(tool_block("update_lead", {"stage": "interested", "note": "cushions"}), stop_reason="tool_use"),
        msg(text_block("Cushion covers ₹1,199 mein hain. Kaunsa colour?")),
        msg(text_block("Hi Priya! Cushion covers ka colour decide hua? Main order mein help kar sakti hoon 😊")),
    ]
    await post_meta(api, meta_payload("cushion covers?", wamid="wamid.f1"))
    await drain()
    job = await db.scalar(select(Job).where(Job.kind == "lead_followup"))
    assert job is not None and job.status == "queued"
    await db.execute(update(Job).where(Job.id == job.id).values(run_at=datetime.now(timezone.utc)))
    await db.commit()
    await drain()
    assert "colour decide" in graph.sent[-1]["text"]["body"]
    conv = await db.scalar(select(Conversation))
    await db.refresh(conv)
    assert conv.followups_sent == 1
    # Follow-up composer sees the transcript as data, without tools.
    assert "tools" not in fake_llm.calls[-1] and "<chat_transcript>" in fake_llm.calls[-1]["messages"][0]["content"]


async def test_meta_permanent_error_is_recorded_not_retried(api, tenant, db, graph, fake_llm: FakeLLM):
    graph.fail_with = {"message": "Re-engagement message", "code": 131047}
    fake_llm.script = [msg(text_block("hello"))]
    await post_meta(api, meta_payload("hi", wamid="wamid.e"))
    await drain()
    out = await db.scalar(select(Message).where(Message.direction == "out"))
    assert out.status == "failed" and "Meta API 400" in out.error
    job = await db.scalar(select(Job).where(Job.kind == "agent_reply"))
    assert job.status == "done"


async def test_status_webhook_updates_message(api, tenant, db, graph, fake_llm):
    fake_llm.script = [msg(text_block("hello"))]
    await post_meta(api, meta_payload("hi", wamid="wamid.st"))
    await drain()
    status_payload = meta_payload("x", wamid="ignored")
    value = status_payload["entry"][0]["changes"][0]["value"]
    value.pop("messages")
    value["statuses"] = [{"id": "wamid.out.1", "status": "read", "recipient_id": CUSTOMER}]
    await post_meta(api, status_payload)
    out = await db.scalar(select(Message).where(Message.direction == "out"))
    await db.refresh(out)
    assert out.status == "read"


async def test_missing_api_key_hands_chat_to_team(api, tenant, db, graph, monkeypatch):
    import anthropic

    from app.agent import llm as llm_mod
    from app.agent.llm import AnthropicLLM

    # A real client with no credentials: the SDK fails before sending anything.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    llm_mod.set_llm(AnthropicLLM(anthropic.AsyncAnthropic()))
    try:
        await post_meta(api, meta_payload("hi", wamid="wamid.nokey"))
        await drain()
    finally:
        llm_mod.set_llm(None)
    conv = await db.scalar(select(Conversation))
    await db.refresh(conv)
    assert conv.mode == "human" and conv.handoff_reason == "AI unavailable: ANTHROPIC_API_KEY is not set"
    job = await db.scalar(select(Job).where(Job.kind == "agent_reply"))
    assert job.status == "done" and job.attempts == 1
    assert [s for s in graph.sent if s["type"] == "text"] == []


async def test_order_not_duplicated_and_reply_redelivered_after_meta_outage(api, tenant, db, graph, mock_store, fake_llm: FakeLLM):
    await sync_catalog(api, tenant.id)
    address = {"name": "Priya Sharma", "address_line1": "12 MI Road", "city": "Jaipur", "state": "Rajasthan", "pincode": "302001"}
    order_call = {"customer": address, "items": [{"product_id": "p2", "quantity": 1}], "customer_confirmed": True}
    fake_llm.script = [
        msg(tool_block("place_cod_order", order_call), stop_reason="tool_use"),
        msg(tool_block("place_cod_order", order_call), stop_reason="tool_use"),  # model repeats itself
        msg(text_block("Order 1001 placed!")),
    ]
    graph.fail_with, graph.fail_status = {"message": "Service temporarily unavailable", "code": 2}, 503
    await post_meta(api, meta_payload("haan confirm", wamid="wamid.dup"))
    await drain()
    assert list(mock_store.state.orders) == ["ord_1001"]
    assert "already placed" in fake_llm.tool_results(2)[0]["content"]
    job = await db.scalar(select(Job).where(Job.kind == "deliver_text"))
    assert job.status == "queued"

    graph.fail_with = None
    await db.execute(update(Job).where(Job.id == job.id).values(run_at=datetime.now(timezone.utc)))
    await db.commit()
    await drain()
    assert graph.sent[-1]["text"]["body"] == "Order 1001 placed!"
    assert len(fake_llm.calls) == 3  # the turn was not re-run
