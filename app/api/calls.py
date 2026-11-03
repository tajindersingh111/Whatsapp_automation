"""Voice calls: how many came in, how many were missed, and the call log."""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.connectors import http_client
from app.db import get_session
from app.models import Call, Contact, Tenant, WhatsAppAccount
from app.security import decrypt, require_admin
from app.whatsapp.client import WhatsAppError, get_calling_settings, set_calling_enabled

router = APIRouter(prefix="/admin/tenants/{tenant_id}", tags=["calls"], dependencies=[Depends(require_admin)])

Outcome = Literal["answered", "missed", "failed", "ringing"]


class CallingToggle(BaseModel):
    enabled: bool


async def _tenant(session: AsyncSession, tenant_id: uuid.UUID) -> Tenant:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="brand not found")
    return tenant


async def _live_account(session: AsyncSession, tenant_id: uuid.UUID) -> WhatsAppAccount | None:
    account = await session.scalar(select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == tenant_id))
    return None if account is None or account.phone_number_id.startswith("sandbox") else account


@router.get("/calls")
async def list_calls(
    tenant_id: uuid.UUID, outcome: Outcome | None = None, days: int = 30, limit: int = 300,
    session: AsyncSession = Depends(get_session),
) -> dict:
    await _tenant(session, tenant_id)
    since = datetime.now(timezone.utc) - timedelta(days=max(1, min(days, 365)))
    where = [Call.tenant_id == tenant_id, Call.started_at >= since]

    totals = dict(
        (await session.execute(
            select(Call.outcome, func.count()).where(*where).group_by(Call.outcome)
        )).all()
    )
    summary = {key: totals.get(key, 0) for key in ("answered", "missed", "failed", "ringing")}
    summary["incoming"] = await session.scalar(
        select(func.count()).select_from(Call).where(*where, Call.direction == "USER_INITIATED")
    ) or 0
    summary["total"] = sum(totals.values())
    summary["talk_minutes"] = round(((await session.scalar(
        select(func.coalesce(func.sum(Call.duration_seconds), 0)).where(*where)
    )) or 0) / 60)
    answered, missed = summary["answered"], summary["missed"]
    summary["answer_rate"] = round(answered / (answered + missed) * 100) if answered + missed else None
    summary["auto_replied"] = await session.scalar(
        select(func.count()).select_from(Call).where(*where, Call.followed_up.is_(True))
    ) or 0

    stmt = select(Call, Contact).join(Contact, Contact.id == Call.contact_id, isouter=True).where(*where)
    if outcome:
        stmt = stmt.where(Call.outcome == outcome)
    rows = (await session.execute(stmt.order_by(Call.started_at.desc()).limit(max(1, min(limit, 1000))))).all()
    return {
        "days": days,
        "summary": summary,
        "calls": [
            {
                "id": c.id, "wa_id": c.wa_id, "name": ct.name if ct else None, "direction": c.direction,
                "incoming": c.direction == "USER_INITIATED", "outcome": c.outcome,
                "started_at": c.started_at, "ended_at": c.ended_at, "duration_seconds": c.duration_seconds,
                "error": c.error, "followed_up": c.followed_up, "follow_up_note": c.follow_up_note,
                "lead_stage": ct.lead_stage if ct else None,
            }
            for c, ct in rows
        ],
    }


@router.get("/calling")
async def calling_settings(tenant_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> dict:
    """Whether the call button is on for this number, as Meta reports it."""
    await _tenant(session, tenant_id)
    account = await _live_account(session, tenant_id)
    if account is None:
        return {"available": False, "status": None, "note": "Connect a real WhatsApp number in Setup to turn calling on."}
    try:
        calling = await get_calling_settings(http_client(), get_settings().meta_graph_version, account.phone_number_id, decrypt(account.access_token_enc))
    except WhatsAppError as exc:
        return {"available": False, "status": None, "note": f"Could not read calling settings from Meta: {exc}"}
    except httpx.HTTPError as exc:
        return {"available": False, "status": None, "note": f"Could not reach Meta: {exc}"}
    restrictions = [r.get("type") for r in (calling.get("restrictions") or {}).get("restrictions_list", [])]
    return {
        "available": True,
        "status": calling.get("status"),
        "call_icon_visibility": calling.get("call_icon_visibility"),
        "callback_permission_status": calling.get("callback_permission_status"),
        "restrictions": restrictions,
        "note": None,
    }


@router.post("/calling")
async def toggle_calling(tenant_id: uuid.UUID, body: CallingToggle, session: AsyncSession = Depends(get_session)) -> dict:
    await _tenant(session, tenant_id)
    account = await _live_account(session, tenant_id)
    if account is None:
        raise HTTPException(status_code=422, detail="Connect a real WhatsApp number in Setup first.")
    try:
        await set_calling_enabled(http_client(), get_settings().meta_graph_version, account.phone_number_id, decrypt(account.access_token_enc), body.enabled)
    except WhatsAppError as exc:
        detail = str(exc)
        if "limit" in detail.lower():
            detail += " Meta requires a messaging limit of at least 2,000 customers/day before calling can be turned on."
        raise HTTPException(status_code=422, detail=detail) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach Meta: {exc}") from exc
    return await calling_settings(tenant_id, session)
