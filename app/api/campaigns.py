"""Bulk campaigns for the dashboard: upload a CSV, pick an approved template, test, then send."""

import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.connectors import http_client
from app.connectors.base import normalize_phone
from app.db import get_session
from app.jobs import enqueue
from app.models import Campaign, CampaignRecipient, Tenant, WhatsAppAccount
from app.security import decrypt, require_admin
from app.services import campaigns as svc
from app.services.messaging import tenant_sender
from app.whatsapp.client import WhatsAppError, list_templates

router = APIRouter(prefix="/admin/tenants/{tenant_id}", tags=["campaigns"], dependencies=[Depends(require_admin)])


class ParamSpec(BaseModel):
    source: str = Field(pattern="^(column|text)$")
    value: str = Field(default="", max_length=500)
    fallback: str = Field(default="", max_length=200)


class TestIn(BaseModel):
    phone: str = Field(min_length=10, max_length=20)


async def _tenant(session: AsyncSession, tenant_id: uuid.UUID) -> Tenant:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="brand not found")
    return tenant


async def _campaign(session: AsyncSession, tenant_id: uuid.UUID, campaign_id: int) -> Campaign:
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None or campaign.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="campaign not found")
    return campaign


async def _view(session: AsyncSession, c: Campaign) -> dict:
    return {
        "id": c.id, "name": c.name, "template_name": c.template_name, "template_language": c.template_language,
        "template_category": c.template_category, "params": c.params, "header_image_url": c.header_image_url,
        "status": c.status, "total": c.total, "counts": await svc.counts(session, c.id),
        "created_at": c.created_at, "started_at": c.started_at, "finished_at": c.finished_at,
    }


@router.get("/templates")
async def templates(tenant_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> dict:
    await _tenant(session, tenant_id)
    account = await session.scalar(select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == tenant_id))
    if account is None or account.phone_number_id.startswith("sandbox") or not account.waba_id:
        return {"templates": [], "note": "Connect WhatsApp (with WABA ID) in Setup to load your approved templates. You can still type a template name."}
    try:
        items = await list_templates(http_client(), get_settings().meta_graph_version, account.waba_id, decrypt(account.access_token_enc))
    except WhatsAppError as exc:
        return {"templates": [], "note": f"Could not load templates from Meta: {exc}"}
    return {"templates": items, "note": None if items else "No approved templates yet. Create one in WhatsApp Manager → Message templates."}


@router.get("/campaigns")
async def list_campaigns(tenant_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> list[dict]:
    rows = (await session.scalars(select(Campaign).where(Campaign.tenant_id == tenant_id).order_by(Campaign.id.desc()).limit(100))).all()
    return [await _view(session, c) for c in rows]


@router.post("/campaigns", status_code=201)
async def create_campaign(
    tenant_id: uuid.UUID,
    name: str = Form(..., min_length=2, max_length=200),
    template_name: str = Form(..., min_length=1, max_length=200),
    template_language: str = Form("en", max_length=20),
    template_category: str | None = Form(None, max_length=30),
    params: str = Form("[]"),
    header_image_url: str | None = Form(None, max_length=1000),
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
) -> dict:
    await _tenant(session, tenant_id)
    try:
        specs = [ParamSpec.model_validate(p).model_dump() for p in json.loads(params or "[]")]
    except (ValueError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=f"invalid params: {exc}") from exc
    try:
        parsed = svc.parse_csv(await file.read())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not parsed.rows:
        raise HTTPException(status_code=422, detail=f"No valid phone numbers found ({len(parsed.invalid)} invalid)")
    missing = [p["value"] for p in specs if p["source"] == "column" and p["value"] not in parsed.columns]
    if missing:
        raise HTTPException(status_code=422, detail=f"CSV has no column named: {', '.join(missing)}")
    campaign = Campaign(
        tenant_id=tenant_id, name=name.strip(), template_name=template_name.strip(), template_language=template_language.strip() or "en",
        template_category=template_category, params=specs, header_image_url=(header_image_url or "").strip() or None, status="draft",
    )
    session.add(campaign)
    await session.flush()
    added, opted_out = await svc.add_recipients(session, campaign, parsed.rows)
    await session.commit()
    preview = [
        {"phone": r["phone"], "name": r["name"], "params": svc.resolve_params(specs, r["name"], r["vars"])} for r in parsed.rows[:5]
    ]
    return {
        **await _view(session, campaign),
        "columns": parsed.columns,
        "valid": added,
        "opted_out": opted_out,
        "duplicates": parsed.duplicates,
        "invalid": len(parsed.invalid),
        "invalid_samples": parsed.invalid[:20],
        "preview": preview,
    }


@router.get("/campaigns/{campaign_id}")
async def get_campaign(tenant_id: uuid.UUID, campaign_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    campaign = await _campaign(session, tenant_id, campaign_id)
    problems = (await session.scalars(
        select(CampaignRecipient)
        .where(CampaignRecipient.campaign_id == campaign_id, CampaignRecipient.status.in_(["failed", "skipped"]))
        .order_by(CampaignRecipient.id)
        .limit(100)
    )).all()
    return {**await _view(session, campaign), "problems": [{"phone": r.phone, "name": r.name, "status": r.status, "error": r.error} for r in problems]}


@router.post("/campaigns/{campaign_id}/test")
async def test_send(tenant_id: uuid.UUID, campaign_id: int, body: TestIn, session: AsyncSession = Depends(get_session)) -> dict:
    tenant = await _tenant(session, tenant_id)
    campaign = await _campaign(session, tenant_id, campaign_id)
    phone = normalize_phone(body.phone)
    if not svc.valid_phone(phone):
        raise HTTPException(status_code=422, detail="invalid phone number")
    sample = await session.scalar(
        select(CampaignRecipient).where(CampaignRecipient.campaign_id == campaign_id).order_by(CampaignRecipient.id).limit(1)
    )
    values = dict(sample.vars) if sample else {}
    try:
        _, msg = await svc.send_to(session, await tenant_sender(session, tenant), tenant, campaign, phone, sample.name if sample else None, values)
    except WhatsAppError as exc:
        raise HTTPException(status_code=502, detail=f"Meta did not accept the message: {exc}") from exc
    await session.commit()
    return {"status": msg.status, "error": msg.error, "body": msg.body}


@router.post("/campaigns/{campaign_id}/start")
async def start(tenant_id: uuid.UUID, campaign_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    campaign = await _campaign(session, tenant_id, campaign_id)
    if campaign.status != "draft":
        raise HTTPException(status_code=409, detail=f"campaign is already {campaign.status}")
    campaign.status = "sending"
    campaign.started_at = datetime.now(timezone.utc)
    await enqueue(session, "campaign_send", {"campaign_id": campaign.id}, tenant_id=tenant_id)
    await session.commit()
    return await _view(session, campaign)


@router.post("/campaigns/{campaign_id}/cancel")
async def cancel(tenant_id: uuid.UUID, campaign_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    campaign = await _campaign(session, tenant_id, campaign_id)
    if campaign.status in ("draft", "sending"):
        campaign.status = "cancelled"
        campaign.finished_at = datetime.now(timezone.utc)
        await session.commit()
    return await _view(session, campaign)
