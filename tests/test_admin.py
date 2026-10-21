from sqlalchemy import select

from app.models import Job, StoreConnection
from app.security import decrypt_json
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
