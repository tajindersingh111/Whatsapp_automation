"""WhatsApp voice calls: the call log, missed-call auto-replies and the calling switch."""

import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models import Call, Contact
from tests.conftest import ADMIN, call_payload, drain, post_meta


async def test_call_log_counts_and_missed_call_auto_reply(api, tenant, db, graph):
    now = int(time.time())
    # 1. A customer calls and the team picks up.
    await post_meta(api, call_payload("919870000001", "wacid.a", "connect", name="Asha"))
    await post_meta(api, call_payload("919870000001", "wacid.a", "terminate", name="Asha",
                                      status="COMPLETED", start_time=str(now - 100), end_time=str(now), duration=100))
    # 2. Another customer calls and nobody picks up: COMPLETED with no duration is a missed call.
    await post_meta(api, call_payload("919870000002", "wacid.b", "connect", name="Bhavna"))
    await post_meta(api, call_payload("919870000002", "wacid.b", "terminate", name="Bhavna", status="COMPLETED"))
    # 3. A call that could not connect at all.
    await post_meta(api, call_payload("919870000003", "wacid.c", "terminate", name="Chetan",
                                      status="FAILED", errors=[{"code": 1000, "message": "User is busy"}]))
    await drain()

    body = (await api.get(f"/admin/tenants/{tenant.id}/calls", headers=ADMIN)).json()
    s = body["summary"]
    assert (s["incoming"], s["answered"], s["missed"], s["failed"]) == (3, 1, 1, 1)
    assert s["talk_minutes"] == 2 and s["answer_rate"] == 50 and s["auto_replied"] == 1

    by_phone = {c["wa_id"]: c for c in body["calls"]}
    assert by_phone["919870000001"]["outcome"] == "answered" and by_phone["919870000001"]["duration_seconds"] == 100
    assert by_phone["919870000001"]["name"] == "Asha"  # the caller becomes a contact
    assert by_phone["919870000002"]["outcome"] == "missed" and by_phone["919870000002"]["followed_up"] is True
    assert "User is busy" in by_phone["919870000003"]["error"] and by_phone["919870000003"]["outcome"] == "failed"

    # The missed caller gets a WhatsApp message so the lead isn't lost; the answered one does not.
    sent = [m for m in graph.sent if m.get("to") == "919870000002"]
    assert sent and "miss ho gaya" in sent[0]["text"]["body"] and "Bhavna" in sent[0]["text"]["body"]
    assert [m for m in graph.sent if m.get("to") == "919870000001"] == []

    only_missed = (await api.get(f"/admin/tenants/{tenant.id}/calls?outcome=missed", headers=ADMIN)).json()
    assert [c["wa_id"] for c in only_missed["calls"]] == ["919870000002"]


async def test_missed_call_reply_respects_optout_and_settings(api, tenant, db, graph):
    db.add(Contact(tenant_id=tenant.id, wa_id="919871000001", opted_out=True))
    await db.commit()
    await post_meta(api, call_payload("919871000001", "wacid.d", "terminate", status="COMPLETED"))
    await drain()
    assert [m for m in graph.sent if m.get("to") == "919871000001"] == []

    cfg = (await api.get(f"/admin/tenants/{tenant.id}/config", headers=ADMIN)).json()
    cfg["missed_call"]["enabled"] = False
    await api.put(f"/admin/tenants/{tenant.id}/config", json=cfg, headers=ADMIN)
    await post_meta(api, call_payload("919871000002", "wacid.e", "terminate", status="COMPLETED"))
    await drain()
    assert [m for m in graph.sent if m.get("to") == "919871000002"] == []
    call = await db.scalar(select(Call).where(Call.wa_call_id == "wacid.e"))
    assert call.outcome == "missed" and call.followed_up is False  # still logged, just not answered


async def test_meta_refusing_the_auto_reply_is_recorded_not_left_as_a_failed_message(api, tenant, db, graph):
    """A call may not open WhatsApp's 24h window. If Meta refuses, record why and leave no failed bubble."""
    from app.models import Message

    graph.fail_with, graph.fail_status = {"message": "Re-engagement message", "code": 131047}, 400
    await post_meta(api, call_payload("919872000002", "wacid.g", "terminate", name="Old", status="COMPLETED"))
    await drain()
    call = await db.scalar(select(Call).where(Call.wa_call_id == "wacid.g"))
    await db.refresh(call)
    assert call.outcome == "missed" and call.followed_up is False
    assert "131047" in (call.follow_up_note or "") and "Could not auto-reply" in call.follow_up_note
    assert await db.scalar(select(Message).where(Message.status == "failed")) is None


async def test_calling_switch(api, tenant, db, graph):
    assert (await api.get(f"/admin/tenants/{tenant.id}/calling", headers=ADMIN)).json()["status"] == "DISABLED"
    r = await api.post(f"/admin/tenants/{tenant.id}/calling", json={"enabled": True}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["status"] == "ENABLED" and graph.calling == "ENABLED"
    assert (await api.post(f"/admin/tenants/{tenant.id}/calling", json={"enabled": False}, headers=ADMIN)).json()["status"] == "DISABLED"

    # Meta refuses while the number's messaging limit is too low: the reason must reach the dashboard.
    graph.calling_blocked = True
    r = await api.post(f"/admin/tenants/{tenant.id}/calling", json={"enabled": True}, headers=ADMIN)
    assert r.status_code == 422 and "2,000" in r.json()["detail"]
