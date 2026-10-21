"""Operator API used by the dashboard (managed-service mode: one admin token for all tenants)."""

import re
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import (
    CorrectionIn,
    HumanMessageIn,
    KnowledgeIn,
    ModeIn,
    PlaygroundIn,
    StoreConnect,
    TenantCreate,
    TenantUpdate,
    WhatsAppConnect,
)
from app.bot_config import BotConfig, load_config
from app.config import get_settings
from app.connectors import REQUIRED_CREDENTIALS, http_client
from app.db import get_session
from app.jobs import enqueue
from app.models import (
    AIUsage,
    Contact,
    Conversation,
    Correction,
    KnowledgeDoc,
    Message,
    Order,
    Product,
    StoreConnection,
    Tenant,
    WhatsAppAccount,
)
from app.pipeline import connector_for, ingest_inbound, respond
from app.security import decrypt, decrypt_json, encrypt, encrypt_json, generate_secret, require_admin
from app.services import catalog
from app.services.conversations import in_service_window
from app.services.messaging import send_message, sender_for
from app.whatsapp.client import SimulatedSender, check_connection, text_message
from app.whatsapp.webhook import InboundMessage

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


async def _tenant(session: AsyncSession, tenant_id: uuid.UUID) -> Tenant:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    return tenant


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "brand"


# --- tenants ---


@router.get("/tenants")
async def list_tenants(session: AsyncSession = Depends(get_session)) -> list[dict]:
    tenants = (await session.scalars(select(Tenant).order_by(Tenant.created_at))).all()
    stores = {s.tenant_id: s.kind for s in (await session.scalars(select(StoreConnection))).all()}
    wa = {a.tenant_id: a.display_phone or a.phone_number_id for a in (await session.scalars(select(WhatsAppAccount))).all()}
    return [
        {"id": str(t.id), "name": t.name, "slug": t.slug, "active": t.active, "store": stores.get(t.id), "whatsapp": wa.get(t.id)}
        for t in tenants
    ]


@router.post("/tenants", status_code=201)
async def create_tenant(body: TenantCreate, session: AsyncSession = Depends(get_session)) -> dict:
    slug = body.slug or _slugify(body.name)
    if await session.scalar(select(Tenant.id).where(Tenant.slug == slug)):
        slug = f"{slug}-{uuid.uuid4().hex[:4]}"
    config = {"brand_name": body.name, **(body.config or {})}
    try:
        BotConfig.model_validate(config)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors(include_url=False)) from exc
    tenant = Tenant(name=body.name, slug=slug, config=config)
    session.add(tenant)
    await session.commit()
    return {"id": str(tenant.id), "slug": tenant.slug}


@router.get("/tenants/{tenant_id}")
async def get_tenant(tenant_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> dict:
    tenant = await _tenant(session, tenant_id)
    account = await session.scalar(select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == tenant_id))
    store = await session.scalar(select(StoreConnection).where(StoreConnection.tenant_id == tenant_id))
    products = await session.scalar(select(func.count()).select_from(Product).where(Product.tenant_id == tenant_id, Product.active.is_(True)))
    base = get_settings().public_base_url.rstrip("/")
    return {
        "id": str(tenant.id),
        "name": tenant.name,
        "slug": tenant.slug,
        "active": tenant.active,
        "config": load_config(tenant.config).model_dump(),
        "whatsapp": {"phone_number_id": account.phone_number_id, "display_phone": account.display_phone, "waba_id": account.waba_id} if account else None,
        "store": {
            "kind": store.kind,
            "base_url": store.base_url,
            "last_synced_at": store.last_synced_at,
            "last_error": store.last_error,
            "webhook_url": f"{base}/webhooks/store/{tenant.id}",
        }
        if store
        else None,
        "active_products": products,
        "meta_webhook_url": f"{base}/webhooks/meta",
        "meta_verify_token": get_settings().meta_verify_token,
    }


@router.patch("/tenants/{tenant_id}")
async def update_tenant(tenant_id: uuid.UUID, body: TenantUpdate, session: AsyncSession = Depends(get_session)) -> dict:
    tenant = await _tenant(session, tenant_id)
    if body.name is not None:
        tenant.name = body.name
    if body.active is not None:
        tenant.active = body.active
    await session.commit()
    return {"ok": True}


@router.get("/tenants/{tenant_id}/config")
async def get_config(tenant_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> dict:
    return load_config((await _tenant(session, tenant_id)).config).model_dump()


@router.put("/tenants/{tenant_id}/config")
async def put_config(tenant_id: uuid.UUID, body: BotConfig, session: AsyncSession = Depends(get_session)) -> dict:
    tenant = await _tenant(session, tenant_id)
    tenant.config = body.model_dump()
    await session.commit()
    return tenant.config


# --- connections ---


@router.put("/tenants/{tenant_id}/whatsapp")
async def connect_whatsapp(tenant_id: uuid.UUID, body: WhatsAppConnect, session: AsyncSession = Depends(get_session)) -> dict:
    await _tenant(session, tenant_id)
    taken = await session.scalar(select(WhatsAppAccount).where(WhatsAppAccount.phone_number_id == body.phone_number_id))
    if taken and taken.tenant_id != tenant_id:
        raise HTTPException(status_code=409, detail="phone_number_id already belongs to another tenant")
    account = await session.scalar(select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == tenant_id))
    if account is None and not body.access_token:
        raise HTTPException(status_code=422, detail="access_token is required")
    if account is None:
        account = WhatsAppAccount(tenant_id=tenant_id, phone_number_id=body.phone_number_id, access_token_enc="")
        session.add(account)
    account.phone_number_id = body.phone_number_id
    account.waba_id = body.waba_id
    account.display_phone = body.display_phone
    if body.access_token:
        account.access_token_enc = encrypt(body.access_token)
    await session.commit()
    return {"ok": True, "check": await _check_whatsapp(account)}


async def _check_whatsapp(account: WhatsAppAccount) -> dict | None:
    if account.phone_number_id.startswith("sandbox"):
        return None
    try:
        return await check_connection(http_client(), get_settings().meta_graph_version, account.phone_number_id, account.waba_id, decrypt(account.access_token_enc))
    except httpx.HTTPError as exc:
        return {"ok": False, "phone": None, "subscribed": False, "problems": [f"Could not reach Meta: {exc}"]}


@router.post("/tenants/{tenant_id}/whatsapp/check")
async def check_whatsapp(tenant_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> dict:
    account = await session.scalar(select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == tenant_id))
    if account is None:
        raise HTTPException(status_code=404, detail="WhatsApp is not connected")
    return {"check": await _check_whatsapp(account)}


@router.delete("/tenants/{tenant_id}/whatsapp")
async def disconnect_whatsapp(tenant_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> dict:
    await session.execute(delete(WhatsAppAccount).where(WhatsAppAccount.tenant_id == tenant_id))
    await session.commit()
    return {"ok": True}


@router.put("/tenants/{tenant_id}/store")
async def connect_store(tenant_id: uuid.UUID, body: StoreConnect, session: AsyncSession = Depends(get_session)) -> dict:
    await _tenant(session, tenant_id)
    conn = await session.scalar(select(StoreConnection).where(StoreConnection.tenant_id == tenant_id))
    creds = {k: v for k, v in body.credentials.items() if v not in (None, "")}
    if conn is not None and conn.kind == body.kind:
        # Re-saving the form must not silently rotate secrets the store is already using.
        creds = {**decrypt_json(conn.credentials_enc), **creds}
    generated = None
    if body.kind == "custom" and ("secret" not in creds or (body.rotate_secret and "secret" not in body.credentials)):
        generated = creds["secret"] = generate_secret()
    missing = [k for k in REQUIRED_CREDENTIALS[body.kind] if k not in creds]
    if missing:
        raise HTTPException(status_code=422, detail=f"missing credentials: {', '.join(missing)}")
    if conn is None:
        conn = StoreConnection(tenant_id=tenant_id, kind=body.kind, base_url=body.base_url, credentials_enc="")
        session.add(conn)
    conn.kind = body.kind
    conn.base_url = body.base_url.rstrip("/")
    conn.credentials_enc = encrypt_json(creds)
    conn.last_error = None
    await session.flush()
    await enqueue(session, "sync_catalog", {"tenant_id": str(tenant_id)}, tenant_id=tenant_id, max_attempts=3)
    await session.commit()
    result = {"ok": True, "webhook_url": f"{get_settings().public_base_url.rstrip('/')}/webhooks/store/{tenant_id}", "sync": "queued"}
    if generated:
        result["shared_secret"] = generated
        result["note"] = "Put this secret in the store's WA_AGENT_SECRET env var. It is shown only once."
    return result


@router.post("/tenants/{tenant_id}/store/sync")
async def sync_store(tenant_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> dict:
    await _tenant(session, tenant_id)
    await enqueue(session, "sync_catalog", {"tenant_id": str(tenant_id)}, tenant_id=tenant_id, max_attempts=3)
    await session.commit()
    return {"ok": True, "sync": "queued"}


@router.get("/tenants/{tenant_id}/products")
async def list_products(tenant_id: uuid.UUID, q: str = "", limit: int = 30, session: AsyncSession = Depends(get_session)) -> list[dict]:
    if q:
        products = await catalog.search_products(session, tenant_id, q, limit=min(limit, 10))
    else:
        products = (await session.scalars(
            select(Product).where(Product.tenant_id == tenant_id, Product.active.is_(True)).order_by(Product.title).limit(min(limit, 200))
        )).all()
    return [{**catalog.product_summary(p), "image_url": p.image_url} for p in products]


# --- bot training: knowledge + corrections ---


@router.get("/tenants/{tenant_id}/knowledge")
async def list_knowledge(tenant_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> list[dict]:
    docs = (await session.scalars(select(KnowledgeDoc).where(KnowledgeDoc.tenant_id == tenant_id).order_by(KnowledgeDoc.id))).all()
    return [{"id": d.id, "title": d.title, "content": d.content, "updated_at": d.updated_at} for d in docs]


@router.post("/tenants/{tenant_id}/knowledge", status_code=201)
async def add_knowledge(tenant_id: uuid.UUID, body: KnowledgeIn, session: AsyncSession = Depends(get_session)) -> dict:
    await _tenant(session, tenant_id)
    doc = KnowledgeDoc(tenant_id=tenant_id, title=body.title, content=body.content)
    session.add(doc)
    await session.commit()
    return {"id": doc.id}


@router.put("/tenants/{tenant_id}/knowledge/{doc_id}")
async def update_knowledge(tenant_id: uuid.UUID, doc_id: int, body: KnowledgeIn, session: AsyncSession = Depends(get_session)) -> dict:
    doc = await session.get(KnowledgeDoc, doc_id)
    if doc is None or doc.tenant_id != tenant_id:
        raise HTTPException(status_code=404)
    doc.title, doc.content = body.title, body.content
    await session.commit()
    return {"ok": True}


@router.delete("/tenants/{tenant_id}/knowledge/{doc_id}")
async def delete_knowledge(tenant_id: uuid.UUID, doc_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    await session.execute(delete(KnowledgeDoc).where(KnowledgeDoc.id == doc_id, KnowledgeDoc.tenant_id == tenant_id))
    await session.commit()
    return {"ok": True}


@router.get("/tenants/{tenant_id}/corrections")
async def list_corrections(tenant_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> list[dict]:
    rows = (await session.scalars(select(Correction).where(Correction.tenant_id == tenant_id).order_by(Correction.id.desc()))).all()
    return [{"id": c.id, "question": c.question, "answer": c.answer, "created_at": c.created_at} for c in rows]


@router.post("/tenants/{tenant_id}/corrections", status_code=201)
async def add_correction(tenant_id: uuid.UUID, body: CorrectionIn, session: AsyncSession = Depends(get_session)) -> dict:
    await _tenant(session, tenant_id)
    row = Correction(tenant_id=tenant_id, question=body.question, answer=body.answer, source_message_id=body.source_message_id)
    session.add(row)
    await session.commit()
    return {"id": row.id}


@router.delete("/tenants/{tenant_id}/corrections/{correction_id}")
async def delete_correction(tenant_id: uuid.UUID, correction_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    await session.execute(delete(Correction).where(Correction.id == correction_id, Correction.tenant_id == tenant_id))
    await session.commit()
    return {"ok": True}


# --- inbox ---


@router.get("/tenants/{tenant_id}/conversations")
async def list_conversations(
    tenant_id: uuid.UUID, mode: str | None = None, q: str = "", include_playground: bool = False, session: AsyncSession = Depends(get_session)
) -> list[dict]:
    stmt = (
        select(Conversation, Contact)
        .join(Contact, Contact.id == Conversation.contact_id)
        .where(Conversation.tenant_id == tenant_id)
        .order_by(Conversation.last_message_at.desc().nulls_last())
        .limit(100)
    )
    if not include_playground:
        stmt = stmt.where(Conversation.is_playground.is_(False))
    if mode:
        stmt = stmt.where(Conversation.mode == mode)
    if q:
        stmt = stmt.where(or_(Contact.wa_id.ilike(f"%{q}%"), Contact.name.ilike(f"%{q}%")))
    rows = (await session.execute(stmt)).all()
    previews: dict[int, Message] = {}
    if rows:
        last_ids = (
            select(func.max(Message.id)).where(Message.conversation_id.in_([c.id for c, _ in rows])).group_by(Message.conversation_id)
        )
        for m in (await session.scalars(select(Message).where(Message.id.in_(last_ids)))).all():
            previews[m.conversation_id] = m
    return [
        {
            "id": c.id,
            "mode": c.mode,
            "handoff_reason": c.handoff_reason,
            "last_message_at": c.last_message_at,
            "contact": {"id": ct.id, "wa_id": ct.wa_id, "name": ct.name, "lead_stage": ct.lead_stage, "lead_note": ct.lead_note, "source": ct.source},
            "can_reply": in_service_window(ct),
            "preview": (previews[c.id].body[:120] if c.id in previews else ""),
        }
        for c, ct in rows
    ]


@router.get("/tenants/{tenant_id}/conversations/{conv_id}/messages")
async def conversation_messages(tenant_id: uuid.UUID, conv_id: int, session: AsyncSession = Depends(get_session)) -> list[dict]:
    conv = await session.get(Conversation, conv_id)
    if conv is None or conv.tenant_id != tenant_id:
        raise HTTPException(status_code=404)
    rows = (await session.scalars(select(Message).where(Message.conversation_id == conv_id).order_by(Message.id.desc()).limit(200))).all()
    return [
        {"id": m.id, "direction": m.direction, "sender": m.sender, "kind": m.kind, "body": m.body, "status": m.status, "error": m.error,
         "created_at": m.created_at, "payload": m.payload if m.direction == "out" else {}}
        for m in reversed(rows)
    ]


@router.post("/tenants/{tenant_id}/conversations/{conv_id}/messages")
async def human_reply(tenant_id: uuid.UUID, conv_id: int, body: HumanMessageIn, session: AsyncSession = Depends(get_session)) -> dict:
    tenant = await _tenant(session, tenant_id)
    conv = await session.get(Conversation, conv_id)
    if conv is None or conv.tenant_id != tenant_id:
        raise HTTPException(status_code=404)
    contact = await session.get(Contact, conv.contact_id)
    assert contact is not None
    if not conv.is_playground and not in_service_window(contact):
        raise HTTPException(status_code=409, detail="More than 24h since the customer's last message. Only approved templates can be sent.")
    sender = await sender_for(session, tenant, conv)
    msg = await send_message(session, sender, conv, contact, text_message(body.text), by="human")
    await session.commit()
    return {"id": msg.id, "status": msg.status, "error": msg.error}


@router.post("/tenants/{tenant_id}/conversations/{conv_id}/mode")
async def set_mode(tenant_id: uuid.UUID, conv_id: int, body: ModeIn, session: AsyncSession = Depends(get_session)) -> dict:
    conv = await session.get(Conversation, conv_id)
    if conv is None or conv.tenant_id != tenant_id:
        raise HTTPException(status_code=404)
    conv.mode = body.mode
    if body.mode == "bot":
        conv.handoff_reason = None
    await session.commit()
    return {"ok": True}


# --- leads, orders, analytics ---


@router.get("/tenants/{tenant_id}/leads")
async def list_leads(tenant_id: uuid.UUID, stage: str | None = None, session: AsyncSession = Depends(get_session)) -> list[dict]:
    stmt = select(Contact).where(Contact.tenant_id == tenant_id, ~Contact.wa_id.startswith("playground")).order_by(Contact.updated_at.desc()).limit(300)
    if stage:
        stmt = stmt.where(Contact.lead_stage == stage)
    rows = (await session.scalars(stmt)).all()
    return [
        {"id": c.id, "wa_id": c.wa_id, "name": c.name, "stage": c.lead_stage, "note": c.lead_note, "source": c.source,
         "last_inbound_at": c.last_inbound_at, "opted_out": c.opted_out}
        for c in rows
    ]


@router.get("/tenants/{tenant_id}/orders")
async def list_orders(tenant_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> list[dict]:
    rows = (await session.scalars(select(Order).where(Order.tenant_id == tenant_id).order_by(Order.id.desc()).limit(200))).all()
    return [
        {"id": o.id, "number": o.number, "status": o.status, "payment_method": o.payment_method, "total": str(o.total),
         "customer_name": o.customer_name, "customer_phone": o.customer_phone, "cod_status": o.cod_status,
         "created_via": o.created_via, "tracking": o.tracking, "created_at": o.created_at}
        for o in rows
    ]


@router.get("/tenants/{tenant_id}/analytics")
async def analytics(tenant_id: uuid.UUID, days: int = 30, session: AsyncSession = Depends(get_session)) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    real = Conversation.is_playground.is_(False)

    async def scalar(stmt):
        return (await session.scalar(stmt)) or 0

    msg_base = select(func.count()).select_from(Message).join(Conversation, Conversation.id == Message.conversation_id).where(
        Message.tenant_id == tenant_id, Message.created_at >= since, real
    )
    leads = dict(
        (await session.execute(
            select(Contact.lead_stage, func.count()).where(Contact.tenant_id == tenant_id, ~Contact.wa_id.startswith("playground")).group_by(Contact.lead_stage)
        )).all()
    )
    cod = dict(
        (await session.execute(
            select(Order.cod_status, func.count()).where(Order.tenant_id == tenant_id, Order.cod_status.is_not(None), Order.created_at >= since).group_by(Order.cod_status)
        )).all()
    )
    bot_orders = (await session.execute(
        select(func.count(), func.coalesce(func.sum(Order.total), 0)).where(Order.tenant_id == tenant_id, Order.created_via == "bot", Order.created_at >= since)
    )).one()
    usage = (await session.execute(
        select(
            func.coalesce(func.sum(AIUsage.input_tokens), 0),
            func.coalesce(func.sum(AIUsage.output_tokens), 0),
            func.coalesce(func.sum(AIUsage.cache_read_tokens), 0),
            func.coalesce(func.sum(AIUsage.cache_write_tokens), 0),
        ).where(AIUsage.tenant_id == tenant_id, AIUsage.created_at >= since)
    )).one()
    return {
        "days": days,
        "conversations": await scalar(
            select(func.count()).select_from(Conversation).where(Conversation.tenant_id == tenant_id, real, Conversation.last_message_at >= since)
        ),
        "messages_in": await scalar(msg_base.where(Message.direction == "in")),
        "bot_replies": await scalar(msg_base.where(Message.direction == "out", Message.sender == "bot")),
        "human_replies": await scalar(msg_base.where(Message.direction == "out", Message.sender == "human")),
        "open_handoffs": await scalar(select(func.count()).select_from(Conversation).where(Conversation.tenant_id == tenant_id, real, Conversation.mode == "human")),
        "leads_by_stage": leads,
        "orders_by_bot": {"count": bot_orders[0], "revenue": str(bot_orders[1])},
        "cod_confirmation": cod,
        "ai_tokens": {"input": usage[0], "output": usage[1], "cache_read": usage[2], "cache_write": usage[3]},
    }


# --- playground: chat with the bot without WhatsApp ---


def _playground_wa_id(session_key: str) -> str:
    return f"playground-{session_key}"


@router.post("/tenants/{tenant_id}/playground")
async def playground(tenant_id: uuid.UUID, body: PlaygroundIn, session: AsyncSession = Depends(get_session)) -> dict:
    tenant = await _tenant(session, tenant_id)
    cfg = load_config(tenant.config)
    if not body.message.strip() and body.button is None:
        raise HTTPException(status_code=422, detail="message or button required")
    m = InboundMessage(
        phone_number_id="playground",
        wa_id=_playground_wa_id(body.session),
        wa_message_id="",
        timestamp=datetime.now(timezone.utc),
        kind="button" if body.button else "text",
        text=(body.button.title if body.button else body.message) or (body.button.id if body.button else ""),
        profile_name="Test Customer",
        button_payload=body.button.id if body.button else None,
    )
    ingested = await ingest_inbound(session, tenant, m, is_playground=True)
    assert ingested is not None
    contact, conv, msg = ingested
    conv.mode = "bot"
    events = await respond(
        session, tenant=tenant, cfg=cfg, contact=contact, conv=conv, sender=SimulatedSender(),
        connector=await connector_for(session, tenant_id), trigger=msg, dry_run=True,
    )
    replies = (await session.scalars(
        select(Message).where(Message.conversation_id == conv.id, Message.direction == "out", Message.id > msg.id).order_by(Message.id)
    )).all()
    await session.commit()
    return {
        "conversation_id": conv.id,
        "reply_mode": cfg.reply_mode,
        "replies": [{"id": r.id, "kind": r.kind, "body": r.body, "payload": r.payload} for r in replies],
        "events": events,
        "lead_stage": contact.lead_stage,
        "handed_off": conv.mode == "human",
    }


@router.delete("/tenants/{tenant_id}/playground/{session_key}")
async def reset_playground(tenant_id: uuid.UUID, session_key: str, session: AsyncSession = Depends(get_session)) -> dict:
    await session.execute(delete(Contact).where(Contact.tenant_id == tenant_id, Contact.wa_id == _playground_wa_id(session_key)))
    await session.commit()
    return {"ok": True}
