"""Audience console: per-customer campaign engagement, exclusions and auto-skip rules."""

import json

from sqlalchemy import select

from app.models import CampaignRecipient
from tests.conftest import ADMIN, drain, meta_payload, post_meta

PEOPLE = {
    "919810000001": "Replier",
    "919810000002": "Reader",
    "919810000003": "Deliveredonly",
    "919810000004": "Singletick",
    "919810000005": "Failer",
}


def csv_for(phones: dict[str, str]) -> bytes:
    return ("name,phone\n" + "".join(f"{n},{p}\n" for p, n in phones.items())).encode()


async def run_campaign(api, tenant_id, phones=PEOPLE, **form) -> int:
    data = {"name": "Promo", "template_name": "promo", "template_language": "en", "template_category": "MARKETING", "params": json.dumps([]), **form}
    r = await api.post(f"/admin/tenants/{tenant_id}/campaigns", data=data, files={"file": ("l.csv", csv_for(phones), "text/csv")}, headers=ADMIN)
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    await api.post(f"/admin/tenants/{tenant_id}/campaigns/{cid}/start", json={}, headers=ADMIN)
    await drain()
    return cid


async def receipts(api, db, cid, updates: dict[str, str]):
    rows = {r.phone: r for r in (await db.scalars(select(CampaignRecipient).where(CampaignRecipient.campaign_id == cid))).all()}
    payload = meta_payload("x", wamid="ignored")
    value = payload["entry"][0]["changes"][0]["value"]
    value.pop("messages")
    value["statuses"] = [{"id": rows[p].wa_message_id, "status": s, "recipient_id": p} for p, s in updates.items()]
    await post_meta(api, payload)


async def test_segments_exclude_and_reuse(api, tenant, db, graph):
    cid = await run_campaign(api, tenant.id)
    await receipts(api, db, cid, {
        "919810000001": "delivered", "919810000002": "read", "919810000003": "delivered", "919810000005": "failed",
    })
    # Replier writes back -> counts as a reply (and as seen).
    await post_meta(api, meta_payload("price kya hai?", wamid="wamid.reply.1", wa_id="919810000001", name="Replier"))
    await drain()

    a = (await api.get(f"/admin/tenants/{tenant.id}/audience", headers=ADMIN)).json()
    assert a["summary"] == {"replied": 1, "read_no_reply": 1, "delivered_unread": 1, "not_delivered": 1, "failed": 1, "excluded": 0}
    seg = {r["phone"]: r["segment"] for r in a["rows"]}
    assert seg == {"919810000001": "replied", "919810000002": "read_no_reply", "919810000003": "delivered_unread",
                   "919810000004": "not_delivered", "919810000005": "failed"}
    detail = (await api.get(f"/admin/tenants/{tenant.id}/campaigns/{cid}", headers=ADMIN)).json()
    assert detail["counts"]["replied"] == 1

    # One click: stop paying for single-tick and failed numbers.
    for segment in ("not_delivered", "failed"):
        r = await api.post(f"/admin/tenants/{tenant.id}/audience/exclude", json={"segment": segment}, headers=ADMIN)
        assert r.json()["excluded"] == 1
    a = (await api.get(f"/admin/tenants/{tenant.id}/audience?segment=excluded", headers=ADMIN)).json()
    assert {r["phone"] for r in a["rows"]} == {"919810000004", "919810000005"}
    assert a["summary"]["excluded"] == 2 and a["summary"]["not_delivered"] == 0

    sent_before = len(graph.sent)
    cid2 = await run_campaign(api, tenant.id)
    new_sends = {s["to"] for s in graph.sent[sent_before:]}
    assert new_sends == {"919810000001", "919810000002", "919810000003"}
    problems = (await api.get(f"/admin/tenants/{tenant.id}/campaigns/{cid2}", headers=ADMIN)).json()["problems"]
    assert {p["phone"] for p in problems if p["error"].startswith("excluded")} == {"919810000004", "919810000005"}

    # Undo works.
    await api.post(f"/admin/tenants/{tenant.id}/audience/include", json={"phones": ["919810000004"]}, headers=ADMIN)
    a = (await api.get(f"/admin/tenants/{tenant.id}/audience", headers=ADMIN)).json()
    assert a["summary"]["excluded"] == 1

    csv_text = (await api.get(f"/admin/tenants/{tenant.id}/audience/export.csv?segment=replied", headers=ADMIN)).text
    lines = csv_text.strip().splitlines()
    assert lines[0].startswith("phone,name,status") and lines[1].startswith("919810000001,Replier,Replied")


async def test_auto_skip_numbers_that_never_get_delivered(api, tenant, db, graph):
    one = {"919820000001": "Ghost", "919820000002": "Fine"}
    for _ in range(2):  # two campaigns; Ghost never gets a delivery receipt
        cid = await run_campaign(api, tenant.id, one)
        await receipts(api, db, cid, {"919820000002": "delivered"})

    r = await api.post(
        f"/admin/tenants/{tenant.id}/campaigns",
        data={"name": "Third", "template_name": "promo", "params": "[]"},
        files={"file": ("l.csv", csv_for(one), "text/csv")}, headers=ADMIN,
    )
    body = r.json()
    assert body["skipped"]["never_delivered"] == 1 and body["counts"]["pending"] == 1

    # The rule can be switched off per campaign.
    r = await api.post(
        f"/admin/tenants/{tenant.id}/campaigns",
        data={"name": "Fourth", "template_name": "promo", "params": "[]", "skip_never_delivered": "false"},
        files={"file": ("l.csv", csv_for(one), "text/csv")}, headers=ADMIN,
    )
    assert r.json()["counts"]["pending"] == 2
