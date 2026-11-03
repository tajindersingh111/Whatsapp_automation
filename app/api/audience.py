"""Audience console: how every customer responds to campaigns, and who to stop paying for."""

import csv
import io
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import CampaignRecipient, Contact, Tenant
from app.security import require_admin
from app.services import campaigns as svc
from app.services.conversations import get_or_create_contact

router = APIRouter(prefix="/admin/tenants/{tenant_id}/audience", tags=["audience"], dependencies=[Depends(require_admin)])

Segment = Literal["replied", "read_no_reply", "delivered_unread", "not_delivered", "meta_limited", "failed", "excluded"]


class ExcludeIn(BaseModel):
    phones: list[str] = Field(default_factory=list, max_length=20000)
    segment: Segment | None = None
    campaign_id: int | None = None  # limit a segment to the people of one campaign
    reason: str = Field(default="", max_length=200)


class IncludeIn(BaseModel):
    phones: list[str] = Field(min_length=1, max_length=20000)


async def _tenant(session: AsyncSession, tenant_id: uuid.UUID) -> Tenant:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="brand not found")
    return tenant


async def _rows(session: AsyncSession, tenant_id: uuid.UUID, campaign_id: int | None = None) -> list[dict]:
    phones = None
    if campaign_id is not None:
        phones = list((await session.scalars(
            select(CampaignRecipient.phone).where(CampaignRecipient.campaign_id == campaign_id, CampaignRecipient.tenant_id == tenant_id)
        )).all())
    history = await svc.engagement_by_phone(session, tenant_id, phones)
    contact_stmt = select(Contact).where(Contact.tenant_id == tenant_id)
    contact_stmt = contact_stmt.where(Contact.wa_id.in_(list(history))) if campaign_id is not None else contact_stmt.where(
        Contact.wa_id.in_(list(history)) | Contact.campaigns_excluded.is_(True)
    )
    contacts = {c.wa_id: c for c in (await session.scalars(contact_stmt)).all()}
    rows = []
    for phone in set(history) | set(contacts):
        h = history.get(phone, svc.Engagement())
        c = contacts.get(phone)
        if h.attempts == 0 and not (c and c.campaigns_excluded):
            continue
        rows.append({
            "phone": phone,
            "name": c.name if c else None,
            "segment": h.segment,
            "attempts": h.attempts,
            "delivered": h.delivered,
            "read": h.read,
            "replied": h.replied,
            "failed": h.failed,
            "last_sent_at": h.last_sent_at,
            "excluded": bool(c and c.campaigns_excluded),
            "excluded_reason": c.excluded_reason if c else None,
            "opted_out": bool(c and c.opted_out),
        })
    rows.sort(key=lambda r: r["last_sent_at"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return rows


def _filter(rows: list[dict], segment: str | None, q: str) -> list[dict]:
    if segment == "excluded":
        rows = [r for r in rows if r["excluded"]]
    elif segment:
        rows = [r for r in rows if r["segment"] == segment and not r["excluded"]]
    if q:
        ql = q.lower()
        rows = [r for r in rows if ql in r["phone"] or ql in (r["name"] or "").lower()]
    return rows


@router.get("")
async def audience(
    tenant_id: uuid.UUID, segment: Segment | None = None, q: str = "", campaign_id: int | None = None, limit: int = 300,
    session: AsyncSession = Depends(get_session),
) -> dict:
    await _tenant(session, tenant_id)
    rows = await _rows(session, tenant_id, campaign_id)
    active = [r for r in rows if not r["excluded"]]
    summary = {key: sum(1 for r in active if r["segment"] == key) for key in svc.SEGMENTS}
    summary["excluded"] = sum(1 for r in rows if r["excluded"])
    filtered = _filter(rows, segment, q)
    return {
        "summary": summary,
        "labels": {**svc.SEGMENTS, "excluded": "Excluded"},
        "reached": len(rows),
        "total": len(filtered),
        "rows": filtered[: max(1, min(limit, 2000))],
    }


@router.post("/exclude")
async def exclude(tenant_id: uuid.UUID, body: ExcludeIn, session: AsyncSession = Depends(get_session)) -> dict:
    await _tenant(session, tenant_id)
    phones = set(body.phones)
    if body.segment:
        phones |= {r["phone"] for r in _filter(await _rows(session, tenant_id, body.campaign_id), body.segment, "")}
    if not phones:
        return {"excluded": 0}
    reason = body.reason.strip() or (svc.SEGMENTS.get(body.segment or "", "") if body.segment else "") or "excluded manually"
    now = datetime.now(timezone.utc)
    for phone in phones:
        contact = await get_or_create_contact(session, tenant_id, phone)
        contact.campaigns_excluded = True
        contact.excluded_reason = reason[:200]
        contact.excluded_at = now
    await session.commit()
    return {"excluded": len(phones)}


@router.post("/include")
async def include(tenant_id: uuid.UUID, body: IncludeIn, session: AsyncSession = Depends(get_session)) -> dict:
    await _tenant(session, tenant_id)
    contacts = (await session.scalars(select(Contact).where(Contact.tenant_id == tenant_id, Contact.wa_id.in_(body.phones)))).all()
    for c in contacts:
        c.campaigns_excluded = False
        c.excluded_reason = None
        c.excluded_at = None
    await session.commit()
    return {"included": len(contacts)}


@router.get("/export.csv")
async def export(tenant_id: uuid.UUID, segment: Segment | None = None, campaign_id: int | None = None, session: AsyncSession = Depends(get_session)):
    await _tenant(session, tenant_id)
    rows = _filter(await _rows(session, tenant_id, campaign_id), segment, "")
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["phone", "name", "status", "messages_sent", "delivered", "seen", "replied", "failed", "last_campaign_at", "excluded", "excluded_reason"])
    for r in rows:
        writer.writerow([
            r["phone"], r["name"] or "", svc.SEGMENTS.get(r["segment"], r["segment"]), r["attempts"], r["delivered"], r["read"], r["replied"],
            r["failed"], r["last_sent_at"].isoformat() if r["last_sent_at"] else "", "yes" if r["excluded"] else "", r["excluded_reason"] or "",
        ])
    name = f"audience-{segment or 'all'}.csv"
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})
