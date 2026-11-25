from sqlalchemy import select

from app.models import Job, StoreConnection
from app.security import decrypt_json
from app.services.conversations import get_or_create_contact, get_or_create_conversation
from app.services.messaging import send_message, sender_for
from app.whatsapp.client import template_message
from tests.conftest import ADMIN, FakeLLM, drain, msg, text_block, tool_block


async def test_admin_requires_token(api):
    assert (await api.get("/admin/tenants")).status_code == 401
    assert (await api.get("/admin/tenants", headers={"Authorization": "Bearer nope"})).status_code == 403


async def test_tenant_setup_training_and_playground(api, db, http, fake_llm: FakeLLM):
    r = await api.post("/admin/tenants", json={"name": "Shatkon Crafts"}, headers=ADMIN)
    assert r.status_code == 201
    tid = r.json()["id"]

    # Connect a custom (Next.js) store: the shared secret is generated and returned once.
    r = await api.put(f"/admin/tenants/{tid}/store", json={"kind": "custom", "base_url": "http://store.test/api/wa-agent"}, headers=ADMIN)
    body = r.json()
    assert r.status_code == 200 and body["webhook_url"] == f"http://agent.test/webhooks/store/{tid}"
    conn = await db.scalar(select(StoreConnection))
    assert decrypt_json(conn.credentials_enc)["secret"] == body["shared_secret"]
    assert await db.scalar(select(Job).where(Job.kind == "sync_catalog"))

    # Shopify without credentials is rejected.
    r = await api.put(f"/admin/tenants/{tid}/store", json={"kind": "shopify", "base_url": "https://x.myshopify.com"}, headers=ADMIN)
    assert r.status_code == 422

    # Train the bot.
    cfg = (await api.get(f"/admin/tenants/{tid}/config", headers=ADMIN)).json()
    cfg["rules"] = ["Always mention free shipping above ₹999."]
    cfg["assistant_name"] = "Meera"
    assert cfg["reply_mode"] == "menu"  # new brands start on the no-AI menu bot
    cfg["reply_mode"] = "ai"
    assert (await api.put(f"/admin/tenants/{tid}/config", json=cfg, headers=ADMIN)).status_code == 200
    bad = {**cfg, "lead_followup": {"after_hours": "soon"}}
    assert (await api.put(f"/admin/tenants/{tid}/config", json=bad, headers=ADMIN)).status_code == 422
    await api.post(f"/admin/tenants/{tid}/knowledge", json={"title": "Returns", "content": "7-day easy returns on unused items."}, headers=ADMIN)
    await api.post(f"/admin/tenants/{tid}/corrections", json={"question": "Do you ship abroad?", "answer": "Abhi sirf India mein delivery hoti hai."}, headers=ADMIN)

    # Playground uses the trained brain and never touches WhatsApp.
    fake_llm.script = [msg(text_block("Namaste! Main Meera. 7 din mein return ho jata hai."))]
    r = await api.post(f"/admin/tenants/{tid}/playground", json={"message": "return policy?"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["replies"][0]["body"].startswith("Namaste")
    brand = fake_llm.calls[0]["system"][1]["text"]
    assert "Meera" in brand and "free shipping above ₹999" in brand and "7-day easy returns" in brand and "Abhi sirf India" in brand

    # Playground conversations are hidden from the inbox and leads.
    assert (await api.get(f"/admin/tenants/{tid}/conversations", headers=ADMIN)).json() == []
    assert (await api.get(f"/admin/tenants/{tid}/leads", headers=ADMIN)).json() == []
    stats = (await api.get(f"/admin/tenants/{tid}/analytics", headers=ADMIN)).json()
    assert stats["ai_tokens"]["input"] == 100 and stats["messages_in"] == 0


async def test_playground_order_is_simulated(api, tenant, db, mock_store, fake_llm: FakeLLM):
    await api.post(f"/admin/tenants/{tenant.id}/store/sync", headers=ADMIN)
    await drain()
    address = {"name": "Test", "address_line1": "1 Test Street", "city": "Jaipur", "state": "RJ", "pincode": "302001"}
    fake_llm.script = [
        msg(tool_block("place_cod_order", {"customer": address, "items": [{"product_id": "p2", "quantity": 1}], "customer_confirmed": True}), stop_reason="tool_use"),
        msg(text_block("Done (test).")),
    ]
    r = await api.post(f"/admin/tenants/{tenant.id}/playground", json={"message": "yes confirm", "session": "t1"}, headers=ADMIN)
    assert r.status_code == 200
    assert any(e["type"] == "order_simulated" for e in r.json()["events"])
    assert mock_store.state.orders == {}


async def test_human_takeover_and_reply(api, tenant, db, graph, fake_llm: FakeLLM):
    from tests.conftest import drain, meta_payload, post_meta

    fake_llm.script = [msg(text_block("Hi!"))]
    await post_meta(api, meta_payload("hello", wamid="wamid.x"))
    await drain()
    convs = (await api.get(f"/admin/tenants/{tenant.id}/conversations", headers=ADMIN)).json()
    assert len(convs) == 1 and convs[0]["can_reply"] and convs[0]["preview"] == "Hi!"
    cid = convs[0]["id"]
    assert (await api.post(f"/admin/tenants/{tenant.id}/conversations/{cid}/mode", json={"mode": "human"}, headers=ADMIN)).status_code == 200
    r = await api.post(f"/admin/tenants/{tenant.id}/conversations/{cid}/messages", json={"text": "Main Anjali, kaise help karun?"}, headers=ADMIN)
    assert r.json()["status"] == "sent"
    assert graph.sent[-1]["text"]["body"] == "Main Anjali, kaise help karun?"
    msgs = (await api.get(f"/admin/tenants/{tenant.id}/conversations/{cid}/messages", headers=ADMIN)).json()
    assert [m["sender"] for m in msgs] == ["customer", "bot", "human"]


async def test_reconnecting_store_keeps_secret_unless_rotated(api, db, http):
    tid = (await api.post("/admin/tenants", json={"name": "Brand"}, headers=ADMIN)).json()["id"]
    first = (await api.put(f"/admin/tenants/{tid}/store", json={"kind": "custom", "base_url": "http://store.test/api/wa-agent"}, headers=ADMIN)).json()
    again = (await api.put(f"/admin/tenants/{tid}/store", json={"kind": "custom", "base_url": "http://store.test/api/wa-agent/"}, headers=ADMIN)).json()
    assert "shared_secret" in first and "shared_secret" not in again
    conn = await db.scalar(select(StoreConnection))
    await db.refresh(conn)
    assert decrypt_json(conn.credentials_enc)["secret"] == first["shared_secret"] and conn.base_url == "http://store.test/api/wa-agent"
    rotated = (await api.put(f"/admin/tenants/{tid}/store", json={"kind": "custom", "base_url": "http://store.test/api/wa-agent", "rotate_secret": True}, headers=ADMIN)).json()
    assert rotated["shared_secret"] != first["shared_secret"]

    # WhatsApp: token required the first time, optional afterwards.
    assert (await api.put(f"/admin/tenants/{tid}/whatsapp", json={"phone_number_id": "123456"}, headers=ADMIN)).status_code == 422
    assert (await api.put(f"/admin/tenants/{tid}/whatsapp", json={"phone_number_id": "123456", "access_token": "EAAG-token-123"}, headers=ADMIN)).status_code == 200
    assert (await api.put(f"/admin/tenants/{tid}/whatsapp", json={"phone_number_id": "123456", "display_phone": "+91 9"}, headers=ADMIN)).status_code == 200


async def test_connect_whatsapp_validates_and_subscribes(api, db, http, graph):
    tid = (await api.post("/admin/tenants", json={"name": "Brand"}, headers=ADMIN)).json()["id"]
    r = await api.put(f"/admin/tenants/{tid}/whatsapp", json={"phone_number_id": "5550001", "waba_id": "777", "access_token": "EAAG-good-token"}, headers=ADMIN)
    check = r.json()["check"]
    assert check["ok"] and check["subscribed"] and check["phone"]["verified_name"] == "Jaipur Linen Co"
    assert graph.subscribed == ["777"]

    r = await api.put(f"/admin/tenants/{tid}/whatsapp", json={"phone_number_id": "5550001", "access_token": "expired-token-x"}, headers=ADMIN)
    assert r.status_code == 200
    from app.models import WhatsAppAccount
    from app.security import encrypt

    account = await db.scalar(select(WhatsAppAccount).where(WhatsAppAccount.phone_number_id == "5550001"))
    account.access_token_enc = encrypt("expired")
    await db.commit()
    check = (await api.post(f"/admin/tenants/{tid}/whatsapp/check", headers=ADMIN)).json()["check"]
    assert not check["ok"] and "expired" in check["problems"][0]

    # The setup page shows the webhook details so nobody has to open .env.
    detail = (await api.get(f"/admin/tenants/{tid}", headers=ADMIN)).json()
    assert detail["meta_webhook_url"] == "http://agent.test/webhooks/meta" and detail["meta_verify_token"] == "verify-me"


async def test_find_my_numbers_from_token(api, db, http, graph):
    tid = (await api.post("/admin/tenants", json={"name": "GG CARRIERS"}, headers=ADMIN)).json()["id"]
    r = await api.post(f"/admin/tenants/{tid}/whatsapp/discover", json={"access_token": "EAAG-good-token"}, headers=ADMIN)
    numbers = r.json()["numbers"]
    real = [n for n in numbers if not n["is_test_number"]]
    assert real == [{"waba_id": "777", "phone_number_id": "5550001", "display_phone_number": "+91 98765 00000",
                     "verified_name": "GG Carriers", "quality_rating": "GREEN", "is_test_number": False}]
    assert any(n["is_test_number"] for n in numbers)  # Meta's +1 555 test number is flagged, not offered

    bad = await api.post(f"/admin/tenants/{tid}/whatsapp/discover", json={"access_token": "bad-token-123"}, headers=ADMIN)
    assert bad.status_code == 422 and "Invalid OAuth" in bad.json()["detail"]


async def test_rename_pause_and_delete_brand(api, db, http):
    tid = (await api.post("/admin/tenants", json={"name": "Arjan logistics"}, headers=ADMIN)).json()["id"]
    r = await api.patch(f"/admin/tenants/{tid}", json={"name": "GG Carriers"}, headers=ADMIN)
    assert r.json()["name"] == "GG Carriers"
    cfg = (await api.get(f"/admin/tenants/{tid}/config", headers=ADMIN)).json()
    assert cfg["brand_name"] == "GG Carriers"  # bot greeting follows the rename

    # A separately customised bot name is left alone.
    cfg["brand_name"] = "GG Carriers Pvt Ltd"
    await api.put(f"/admin/tenants/{tid}/config", json=cfg, headers=ADMIN)
    await api.patch(f"/admin/tenants/{tid}", json={"name": "GG"}, headers=ADMIN)
    assert (await api.get(f"/admin/tenants/{tid}/config", headers=ADMIN)).json()["brand_name"] == "GG Carriers Pvt Ltd"

    assert (await api.patch(f"/admin/tenants/{tid}", json={"active": False}, headers=ADMIN)).json()["active"] is False
    assert (await api.patch(f"/admin/tenants/{tid}", json={"name": "x"}, headers=ADMIN)).status_code == 422

    await api.post(f"/admin/tenants/{tid}/knowledge", json={"title": "Returns", "content": "7 days"}, headers=ADMIN)
    bad = await api.request("DELETE", f"/admin/tenants/{tid}", params={"confirm": "wrong"}, headers=ADMIN)
    assert bad.status_code == 422
    ok = await api.request("DELETE", f"/admin/tenants/{tid}", params={"confirm": "GG"}, headers=ADMIN)
    assert ok.status_code == 200
    assert (await api.get(f"/admin/tenants/{tid}", headers=ADMIN)).status_code == 404
    assert (await api.get("/admin/tenants", headers=ADMIN)).json() == []


async def test_inbox_views_separate_real_conversations(api, tenant, db, graph, fake_llm):
    from tests.conftest import drain, meta_payload, post_meta

    fake_llm.script = [msg(text_block("Ji bataiye")), msg(text_block("Theek hai"))]
    # Two customers write in; the bot answers both.
    await post_meta(api, meta_payload("rate kya hai?", wamid="w.a1", wa_id="919861000001", name="Chatty"))
    await post_meta(api, meta_payload("hello", wamid="w.b1", wa_id="919861000002", name="Waiting"))
    await drain()
    # A campaign-style message to someone who never writes back.
    silent = await get_or_create_contact(db, tenant.id, "919861000003", "Silent", source="campaign: Diwali")
    conv = await get_or_create_conversation(db, tenant.id, silent)
    blast = template_message("diwali_offer", "en", ["Silent"])  # campaigns always send templates
    await send_message(db, await sender_for(db, tenant, conv), conv, silent, blast, by="campaign")
    await db.commit()

    view = lambda v: api.get(f"/admin/tenants/{tenant.id}/conversations?view={v}", headers=ADMIN)
    phones = lambda r: {c["contact"]["wa_id"] for c in r.json()}

    assert phones(await view("all")) == {"919861000001", "919861000002", "919861000003"}
    assert phones(await view("chatting")) == {"919861000001", "919861000002"}  # they wrote to us
    assert phones(await view("no_reply")) == {"919861000003"}  # only we wrote
    assert phones(await view("waiting")) == set()  # the bot answered everyone

    # The second customer writes again; the bot is off, so nobody answers: they are waiting.
    await api.post(f"/admin/tenants/{tenant.id}/conversations/{(await view('chatting')).json()[0]['id']}/mode", json={"mode": "human"}, headers=ADMIN)
    await post_meta(api, meta_payload("koi hai?", wamid="w.b2", wa_id="919861000002", name="Waiting"))
    await drain()
    waiting = (await view("waiting")).json()
    assert [c["contact"]["wa_id"] for c in waiting] == ["919861000002"]
    assert waiting[0]["waiting"] is True and waiting[0]["replied"] is True and waiting[0]["preview"] == "koi hai?"

    summary = (await api.get(f"/admin/tenants/{tenant.id}/conversations/summary", headers=ADMIN)).json()
    assert summary == {"all": 3, "waiting": 1, "chatting": 2, "human": 1, "no_reply": 1, "hot": 0}
    assert (await view("human")).json()[0]["mode"] == "human"
    assert phones(await api.get(f"/admin/tenants/{tenant.id}/conversations?view=all&q=Silent", headers=ADMIN)) == {"919861000003"}
    assert (await view("nonsense")).status_code == 422
