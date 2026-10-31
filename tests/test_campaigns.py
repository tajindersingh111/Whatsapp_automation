"""Bulk campaigns: CSV parsing, opt-outs, test send, throttled sending, delivery status, pause on rate limits."""

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, update

from app.models import Campaign, CampaignRecipient, Contact, Job, WhatsAppAccount
from app.services.campaigns import parse_csv, resolve_params
from tests.conftest import ADMIN, drain, meta_payload, post_meta

CSV = (
    "Name,Phone,City,Coupon\n"
    "Priya Sharma,+91 98765 43210,Jaipur,DIWALI20\n"
    "Rahul,9811122233,Delhi,\n"
    "Duplicate Priya,09876543210,Jaipur,X\n"
    "Bad,12345,Nowhere,\n"
    "Opted Out,9800000009,Pune,\n"
).encode()


def test_parse_csv_variants():
    parsed = parse_csv(CSV)
    assert [r["phone"] for r in parsed.rows] == ["919876543210", "919811122233", "919800000009"]
    assert parsed.duplicates == 1 and parsed.invalid == ["12345"]
    assert parsed.rows[0]["vars"]["Coupon"] == "DIWALI20" and parsed.rows[0]["name"] == "Priya Sharma"

    no_header = parse_csv(b"9876543210,Asha\n9811122233\n")
    assert [(r["phone"], r["name"]) for r in no_header.rows] == [("919876543210", "Asha"), ("919811122233", None)]

    excel = parse_csv("﻿mobile;customer name\n98765 43210;Neha\n".encode("utf-8"))
    assert excel.rows[0]["phone"] == "919876543210" and excel.rows[0]["name"] == "Neha"

    with pytest.raises(ValueError, match="phone"):
        parse_csv(b"name,city\nA,B\n")


def test_resolve_params_never_empty():
    specs = [
        {"source": "column", "value": "Name", "fallback": "Customer"},
        {"source": "column", "value": "Coupon", "fallback": "WELCOME10"},
        {"source": "text", "value": "20%"},
    ]
    assert resolve_params(specs, "Priya", {"Name": "Priya", "Coupon": "DIWALI20"}) == ["Priya", "DIWALI20", "20%"]
    assert resolve_params(specs, None, {"Name": "", "Coupon": ""}) == ["Customer", "WELCOME10", "20%"]


async def create(api, tenant_id, csv_bytes=CSV, **form):
    data = {
        "name": "Diwali sale",
        "template_name": "diwali_offer",
        "template_language": "en",
        "template_category": "MARKETING",
        "params": json.dumps([{"source": "column", "value": "Name", "fallback": "Customer"}, {"source": "column", "value": "Coupon", "fallback": "FIRST10"}]),
        **form,
    }
    return await api.post(f"/admin/tenants/{tenant_id}/campaigns", data=data, files={"file": ("list.csv", csv_bytes, "text/csv")}, headers=ADMIN)


async def test_campaign_end_to_end(api, tenant, db, graph):
    db.add(Contact(tenant_id=tenant.id, wa_id="919800000009", opted_out=True))
    await db.commit()

    r = await create(api, tenant.id)
    assert r.status_code == 201, r.text
    body = r.json()
    assert (body["valid"], body["opted_out"], body["duplicates"], body["invalid"]) == (3, 1, 1, 1)
    assert body["preview"][1]["params"] == ["Rahul", "FIRST10"]  # empty coupon -> fallback
    assert body["counts"]["pending"] == 2 and body["counts"]["skipped"] == 1
    cid = body["id"]

    # Test send goes to one number only and nothing else starts.
    t = await api.post(f"/admin/tenants/{tenant.id}/campaigns/{cid}/test", json={"phone": "9999911111"}, headers=ADMIN)
    assert t.json()["status"] == "sent"
    assert graph.sent[-1]["to"] == "919999911111" and graph.sent[-1]["template"]["name"] == "diwali_offer"

    r = await api.post(f"/admin/tenants/{tenant.id}/campaigns/{cid}/start", json={}, headers=ADMIN)
    assert r.json()["status"] == "sending"
    assert (await api.post(f"/admin/tenants/{tenant.id}/campaigns/{cid}/start", json={}, headers=ADMIN)).status_code == 409
    await drain()

    sent = [s for s in graph.sent if s["to"] in ("919876543210", "919811122233")]
    assert len(sent) == 2
    params = [p["text"] for p in sent[0]["template"]["components"][0]["parameters"]]
    assert params == ["Priya Sharma", "DIWALI20"]
    assert not any(s["to"] == "919800000009" for s in graph.sent)  # opted out

    detail = (await api.get(f"/admin/tenants/{tenant.id}/campaigns/{cid}", headers=ADMIN)).json()
    assert detail["status"] == "done" and detail["counts"]["sent"] == 2
    assert detail["problems"] == [{"phone": "919800000009", "name": "Opted Out", "status": "skipped", "error": "customer opted out (STOP)"}]

    # Delivery receipts from Meta update the campaign numbers.
    rec = await db.scalar(select(CampaignRecipient).where(CampaignRecipient.phone == "919876543210"))
    payload = meta_payload("x", wamid="ignored")
    value = payload["entry"][0]["changes"][0]["value"]
    value.pop("messages")
    value["statuses"] = [{"id": rec.wa_message_id, "status": "read", "recipient_id": "919876543210"}]
    await post_meta(api, payload)
    detail = (await api.get(f"/admin/tenants/{tenant.id}/campaigns/{cid}", headers=ADMIN)).json()
    assert detail["counts"]["read"] == 1 and detail["counts"]["sent"] == 1

    # Campaign messages show up in each customer's conversation, so replies have context.
    contact = await db.scalar(select(Contact).where(Contact.wa_id == "919811122233"))
    assert contact.source == "campaign: Diwali sale"


async def test_rate_limit_pauses_without_failing_customers(api, tenant, db, graph):
    r = await create(api, tenant.id)
    cid = r.json()["id"]
    graph.fail_after = 1  # Meta accepts one message, then rate-limits
    await api.post(f"/admin/tenants/{tenant.id}/campaigns/{cid}/start", json={}, headers=ADMIN)
    await drain()
    counts = (await api.get(f"/admin/tenants/{tenant.id}/campaigns/{cid}", headers=ADMIN)).json()["counts"]
    assert counts["sent"] == 1 and counts["pending"] == 2 and counts["failed"] == 0
    job = await db.scalar(select(Job).where(Job.kind == "campaign_send", Job.status == "queued"))
    assert job is not None and job.run_at > datetime.now(timezone.utc)

    graph.fail_after = None
    await db.execute(update(Job).where(Job.id == job.id).values(run_at=datetime.now(timezone.utc)))
    await db.commit()
    await drain()
    counts = (await api.get(f"/admin/tenants/{tenant.id}/campaigns/{cid}", headers=ADMIN)).json()["counts"]
    assert counts["sent"] == 3 and counts["pending"] == 0
    assert len(graph.sent) == 3  # nobody got the message twice


async def test_cancel_stops_sending(api, tenant, db, graph):
    cid = (await create(api, tenant.id)).json()["id"]
    await api.post(f"/admin/tenants/{tenant.id}/campaigns/{cid}/start", json={}, headers=ADMIN)
    r = await api.post(f"/admin/tenants/{tenant.id}/campaigns/{cid}/cancel", json={}, headers=ADMIN)
    assert r.json()["status"] == "cancelled"
    await drain()
    assert graph.sent == []


async def test_bad_inputs_and_templates(api, tenant, db, graph):
    r = await create(api, tenant.id, csv_bytes=b"name,city\nA,B\n")
    assert r.status_code == 422 and "phone" in r.json()["detail"]
    r = await create(api, tenant.id, params=json.dumps([{"source": "column", "value": "Missing"}]))
    assert r.status_code == 422 and "Missing" in r.json()["detail"]

    assert (await api.get(f"/admin/tenants/{tenant.id}/templates", headers=ADMIN)).json()["templates"] == []  # no WABA id yet
    account = await db.scalar(select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == tenant.id))
    account.waba_id = "999"
    await db.commit()
    graph.templates = [
        {"name": "diwali_offer", "language": "en", "status": "APPROVED", "category": "MARKETING",
         "components": [{"type": "HEADER", "format": "IMAGE"}, {"type": "BODY", "text": "Hi {{1}}, use {{2}} for 20% off. {{1}}, hurry!"}]},
        {"name": "draft_one", "language": "en", "status": "PENDING", "components": []},
    ]
    body = (await api.get(f"/admin/tenants/{tenant.id}/templates", headers=ADMIN)).json()
    # Pending templates are listed too (so the user isn't confused), approved first.
    assert [(t["name"], t["status"], t["approved"]) for t in body["templates"]] == [("diwali_offer", "APPROVED", True), ("draft_one", "PENDING", False)]
    assert body["approved_count"] == 1 and body["pending_count"] == 1
    approved = body["templates"][0]
    assert approved["param_count"] == 2 and approved["header"] == "IMAGE" and approved["category"] == "MARKETING"

    # Image header templates carry the image link.
    cid = (await create(api, tenant.id, header_image_url="https://cdn.example.com/diwali.jpg")).json()["id"]
    await api.post(f"/admin/tenants/{tenant.id}/campaigns/{cid}/test", json={"phone": "9999911111"}, headers=ADMIN)
    assert graph.sent[-1]["template"]["components"][0] == {"type": "header", "parameters": [{"type": "image", "image": {"link": "https://cdn.example.com/diwali.jpg"}}]}
    assert (await db.scalar(select(Campaign).where(Campaign.id == cid))).status == "draft"
