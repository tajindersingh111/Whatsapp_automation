"""Job handlers: everything that happens after a webhook arrives."""

import base64
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.runner import compose_followup, run_agent
from app.bot_config import BotConfig, load_config
from app.connectors import StoreError, build_connector
from app.connectors.base import StoreConnector
from app.jobs import Job, PermanentJobError, enqueue, handler
from app.menu_bot import menu_followup, run_menu_bot
from app.models import Campaign, Contact, Conversation, Message, Order, StoreConnection, Tenant, WebhookEvent, WhatsAppAccount
from app.services import campaigns as campaign_service
from app.services import catalog
from app.services.conversations import (
    get_or_create_contact,
    get_or_create_conversation,
    in_service_window,
    latest_inbound,
    record_message,
)
from app.services.messaging import OutsideServiceWindow, send_message, sender_for, tenant_sender
from app.services.orders import (
    find_order,
    handle_cod_reply,
    maybe_notify_shipping,
    send_cod_template,
    send_shipping_template,
    start_cod_confirmation,
    upsert_order,
)
from app.whatsapp.client import Sender, text_message
from app.whatsapp.webhook import InboundMessage

log = logging.getLogger(__name__)

OPT_OUT_WORDS = {"stop", "unsubscribe"}
OPT_IN_WORDS = {"start", "subscribe"}
FOLLOWUP_STAGES = {"browsing", "interested", "checkout_sent"}


async def connector_for(session: AsyncSession, tenant_id: uuid.UUID) -> StoreConnector | None:
    conn = await session.scalar(select(StoreConnection).where(StoreConnection.tenant_id == tenant_id))
    return build_connector(conn) if conn else None


async def _load(session: AsyncSession, conversation_id: int) -> tuple[Tenant, Contact, Conversation]:
    conv = await session.get(Conversation, conversation_id)
    if conv is None:
        raise PermanentJobError(f"conversation {conversation_id} not found")
    contact = await session.get(Contact, conv.contact_id)
    tenant = await session.get(Tenant, conv.tenant_id)
    assert contact is not None and tenant is not None
    return tenant, contact, conv


async def _seen(session: AsyncSession, tenant_id: uuid.UUID | None, source: str, key: str) -> bool:
    """Record an event key; True if it was already processed."""
    result = await session.execute(
        insert(WebhookEvent)
        .values(tenant_id=tenant_id, source=source, event_key=key[:300])
        .on_conflict_do_nothing(index_elements=["event_key"])
        .returning(WebhookEvent.id)
    )
    return result.scalar_one_or_none() is None


# --- inbound WhatsApp messages ---


def serialize_inbound(m: InboundMessage) -> dict:
    return {
        "phone_number_id": m.phone_number_id,
        "wa_id": m.wa_id,
        "wa_message_id": m.wa_message_id,
        "timestamp": m.timestamp.isoformat(),
        "kind": m.kind,
        "text": m.text,
        "profile_name": m.profile_name,
        "button_payload": m.button_payload,
        "referral": m.referral,
        "context_message_id": m.context_message_id,
    }


def _referral_source(referral: dict | None) -> str | None:
    if not referral:
        return None
    label = referral.get("headline") or referral.get("source_url") or ""
    return f"{referral.get('source_type', 'ad')} {referral.get('source_id', '')} {label}".strip()[:100]


async def ingest_inbound(session: AsyncSession, tenant: Tenant, m: InboundMessage, *, is_playground: bool = False) -> tuple[Contact, Conversation, Message] | None:
    """Store the customer's message. Returns None if it was a duplicate delivery."""
    if m.wa_message_id:
        exists = await session.scalar(select(Message.id).where(Message.wa_message_id == m.wa_message_id))
        if exists:
            return None
    contact = await get_or_create_contact(session, tenant.id, m.wa_id, m.profile_name, source=_referral_source(m.referral))
    if contact.last_inbound_at is None or m.timestamp > contact.last_inbound_at:
        contact.last_inbound_at = m.timestamp
    if contact.lead_stage == "new":
        contact.lead_stage = "browsing"
    conv = await get_or_create_conversation(session, tenant.id, contact, is_playground=is_playground)
    payload = {"button_payload": m.button_payload, "referral": m.referral, "context": m.context_message_id}
    msg = await record_message(
        session,
        conv,
        direction="in",
        sender="customer",
        body=m.text,
        kind=m.kind,
        payload={k: v for k, v in payload.items() if v},
        wa_message_id=m.wa_message_id or None,
        created_at=m.timestamp,
    )
    if not is_playground:
        await campaign_service.mark_replied(session, tenant.id, contact.wa_id, m.timestamp)
    return contact, conv, msg


@handler("inbound_message")
async def handle_inbound(session: AsyncSession, job: Job) -> None:
    data = job.payload
    account = await session.scalar(select(WhatsAppAccount).where(WhatsAppAccount.phone_number_id == data["phone_number_id"]))
    if account is None:
        log.warning("message for unknown phone_number_id %s dropped", data["phone_number_id"])
        return
    tenant = await session.get(Tenant, account.tenant_id)
    assert tenant is not None
    m = InboundMessage(
        phone_number_id=data["phone_number_id"],
        wa_id=data["wa_id"],
        wa_message_id=data["wa_message_id"],
        timestamp=datetime.fromisoformat(data["timestamp"]),
        kind=data["kind"],
        text=data["text"],
        profile_name=data.get("profile_name"),
        button_payload=data.get("button_payload"),
        referral=data.get("referral"),
        context_message_id=data.get("context_message_id"),
    )
    ingested = await ingest_inbound(session, tenant, m)
    if ingested is None:
        return
    contact, conv, msg = ingested
    await after_inbound(session, tenant, contact, conv, msg)
    sender = await sender_for(session, tenant, conv)
    await sender.mark_read(m.wa_message_id)


async def after_inbound(session: AsyncSession, tenant: Tenant, contact: Contact, conv: Conversation, msg: Message) -> None:
    """Decide what happens with a freshly stored customer message."""
    cfg = load_config(tenant.config)
    word = msg.body.strip().lower()
    if word in OPT_OUT_WORDS:
        contact.opted_out = True
        sender = await sender_for(session, tenant, conv)
        await send_message(session, sender, conv, contact, text_message("You're unsubscribed. Reply START anytime to chat again."), by="system")
        return
    if word in OPT_IN_WORDS and contact.opted_out:
        contact.opted_out = False
    if contact.opted_out:
        return

    button = (msg.payload or {}).get("button_payload") or ""
    if button.startswith("cod:"):
        sender = await sender_for(session, tenant, conv)
        connector = await connector_for(session, tenant.id)
        if await handle_cod_reply(session, tenant, cfg, connector, sender, contact, conv, button):
            return

    if tenant.active and cfg.enabled and conv.mode == "bot":
        from app.config import get_settings

        await enqueue(
            session,
            "agent_reply",
            {"conversation_id": conv.id, "trigger_message_id": msg.id},
            tenant_id=tenant.id,
            delay_seconds=get_settings().inbound_debounce_seconds,
            dedupe_key=f"reply:{msg.id}",
            max_attempts=3,
        )


@handler("agent_reply")
async def handle_agent_reply(session: AsyncSession, job: Job) -> None:
    conv_id = job.payload["conversation_id"]
    trigger_id = job.payload["trigger_message_id"]
    # One reply at a time per conversation, even with several workers.
    await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": conv_id})
    tenant, contact, conv = await _load(session, conv_id)
    cfg = load_config(tenant.config)
    if conv.mode != "bot" or not cfg.enabled or not tenant.active or contact.opted_out:
        return
    latest = await latest_inbound(session, conv.id)
    if latest is None or latest.id != trigger_id:
        return  # a newer customer message has its own job; answer everything once, then
    answered = await session.scalar(
        select(Message.id).where(Message.conversation_id == conv.id, Message.direction == "out", Message.sender == "bot", Message.id > trigger_id).limit(1)
    )
    if answered:
        return
    sender = await sender_for(session, tenant, conv)
    connector = await connector_for(session, tenant.id)
    try:
        await respond(session, tenant=tenant, cfg=cfg, contact=contact, conv=conv, sender=sender, connector=connector, trigger=latest)
    except OutsideServiceWindow as exc:
        raise PermanentJobError(str(exc)) from exc
    await schedule_followup(session, tenant, contact, conv, trigger_id)


async def respond(
    session: AsyncSession,
    *,
    tenant: Tenant,
    cfg: BotConfig,
    contact: Contact,
    conv: Conversation,
    sender: Sender,
    connector: StoreConnector | None,
    trigger: Message,
    dry_run: bool = False,
) -> list[dict]:
    """Reply to the customer's latest message with the brand's chosen bot. Returns events for the UI."""
    if cfg.reply_mode == "menu":
        result = await run_menu_bot(session, tenant=tenant, cfg=cfg, contact=contact, conv=conv, sender=sender, connector=connector, message=trigger)
        return result.events
    agent = await run_agent(session, tenant=tenant, cfg=cfg, contact=contact, conv=conv, sender=sender, connector=connector, dry_run=dry_run)
    return agent.events


async def schedule_followup(session: AsyncSession, tenant: Tenant, contact: Contact, conv: Conversation, trigger_id: int) -> None:
    flow = load_config(tenant.config).lead_followup
    if flow.enabled and contact.lead_stage in FOLLOWUP_STAGES and conv.followups_sent < flow.max_followups and conv.mode == "bot":
        await enqueue(
            session,
            "lead_followup",
            {"conversation_id": conv.id, "after_message_id": trigger_id},
            tenant_id=tenant.id,
            delay_seconds=flow.after_hours * 3600,
            dedupe_key=f"followup:{trigger_id}",
            max_attempts=2,
        )


@handler("lead_followup")
async def handle_lead_followup(session: AsyncSession, job: Job) -> None:
    tenant, contact, conv = await _load(session, job.payload["conversation_id"])
    cfg = load_config(tenant.config)
    latest = await latest_inbound(session, conv.id)
    if (
        not cfg.enabled
        or not cfg.lead_followup.enabled
        or conv.mode != "bot"
        or contact.opted_out
        or contact.lead_stage not in FOLLOWUP_STAGES
        or conv.followups_sent >= cfg.lead_followup.max_followups
        or latest is None
        or latest.id != job.payload["after_message_id"]
        or not in_service_window(contact)
    ):
        return
    if cfg.reply_mode == "menu":
        last = (conv.state or {}).get("last_product")
        product = await catalog.get_product(session, tenant.id, last) if last else None
        message = menu_followup(cfg, contact, conv, product if product and product.active else None)
    else:
        text_reply = await compose_followup(session, tenant=tenant, cfg=cfg, contact=contact, conv=conv)
        if not text_reply:
            return
        message = text_message(text_reply)
    sender = await sender_for(session, tenant, conv)
    await send_message(session, sender, conv, contact, message, by="bot")
    conv.followups_sent += 1


@handler("deliver_text")
async def handle_deliver_text(session: AsyncSession, job: Job) -> None:
    tenant, contact, conv = await _load(session, job.payload["conversation_id"])
    sender = await sender_for(session, tenant, conv)
    try:
        await send_message(session, sender, conv, contact, text_message(job.payload["text"]), by=job.payload.get("by", "bot"))
    except OutsideServiceWindow as exc:
        raise PermanentJobError(str(exc)) from exc


# --- COD + shipping automations ---


@handler("cod_send_confirmation")
async def handle_cod_send(session: AsyncSession, job: Job) -> None:
    order = await find_order(session, job.payload["order_id"])
    tenant = await session.get(Tenant, order.tenant_id)
    assert tenant is not None
    if order.cod_status != "pending":
        return
    conv = await _order_conversation(session, tenant, order)
    sender = await sender_for(session, tenant, conv) if conv else None
    if sender is None:
        order.cod_status = "no_phone"
        return
    await send_cod_template(session, sender, tenant, order)
    flow = load_config(tenant.config).cod_confirmation
    await enqueue(
        session, "cod_reminder", {"order_id": order.id, "n": 1}, tenant_id=tenant.id,
        delay_seconds=flow.reminder_after_hours * 3600, dedupe_key=f"cod-reminder:{order.id}:1",
    )


@handler("cod_reminder")
async def handle_cod_reminder(session: AsyncSession, job: Job) -> None:
    order = await find_order(session, job.payload["order_id"])
    if order.cod_status != "pending":
        return
    tenant = await session.get(Tenant, order.tenant_id)
    assert tenant is not None
    flow = load_config(tenant.config).cod_confirmation
    n = int(job.payload["n"])
    if n > flow.max_reminders:
        order.cod_status = "no_response"
        return
    conv = await _order_conversation(session, tenant, order)
    if conv is None:
        return
    await send_cod_template(session, await sender_for(session, tenant, conv), tenant, order)
    order.cod_reminders_sent = n
    await enqueue(
        session, "cod_reminder", {"order_id": order.id, "n": n + 1}, tenant_id=tenant.id,
        delay_seconds=flow.reminder_after_hours * 3600, dedupe_key=f"cod-reminder:{order.id}:{n + 1}",
    )


@handler("shipping_update")
async def handle_shipping_update(session: AsyncSession, job: Job) -> None:
    order = await find_order(session, job.payload["order_id"])
    tenant = await session.get(Tenant, order.tenant_id)
    assert tenant is not None
    conv = await _order_conversation(session, tenant, order)
    if conv is None:
        return
    await send_shipping_template(session, await sender_for(session, tenant, conv), tenant, order)


async def _order_conversation(session: AsyncSession, tenant: Tenant, order: Order) -> Conversation | None:
    if order.contact_id is None:
        if not order.customer_phone:
            return None
        contact = await get_or_create_contact(session, tenant.id, order.customer_phone, order.customer_name, source="store order")
        order.contact_id = contact.id
    contact = await session.get(Contact, order.contact_id)
    if contact is None:
        return None
    return await get_or_create_conversation(session, tenant.id, contact)


# --- store webhooks + catalog sync ---


@handler("store_webhook")
async def handle_store_webhook(session: AsyncSession, job: Job) -> None:
    tenant = await session.get(Tenant, uuid.UUID(job.payload["tenant_id"]))
    if tenant is None:
        raise PermanentJobError("tenant not found")
    connector = await connector_for(session, tenant.id)
    if connector is None:
        raise PermanentJobError("store not connected")
    body = base64.b64decode(job.payload["body_b64"])
    for event in connector.parse_webhook(job.payload["headers"], body):
        if await _seen(session, tenant.id, connector.kind, f"{tenant.id}:{event.event_id}"):
            continue
        await apply_store_event(session, tenant, event)


async def apply_store_event(session: AsyncSession, tenant: Tenant, event) -> None:
    if event.type == "product.updated" and event.product:
        await catalog.upsert_product(session, tenant.id, event.product)
    elif event.type == "product.deleted" and event.product_id:
        await catalog.deactivate_product(session, tenant.id, event.product_id)
    elif event.type in ("order.created", "order.updated") and event.order:
        contact_id = None
        if event.order.customer_phone:
            contact = await get_or_create_contact(session, tenant.id, event.order.customer_phone, event.order.customer_name, source="store order")
            contact_id = contact.id
            if contact.lead_stage not in ("ordered",):
                contact.lead_stage = "ordered"
        order, created = await upsert_order(session, tenant.id, event.order, contact_id=contact_id)
        if order.status == "cancelled" and order.cod_status == "pending":
            order.cod_status = "cancelled"
        if created and order.created_via == "store":
            await start_cod_confirmation(session, tenant, order)
        await maybe_notify_shipping(session, tenant, order)


@handler("sync_catalog")
async def handle_sync_catalog(session: AsyncSession, job: Job) -> None:
    tenant_id = uuid.UUID(job.payload["tenant_id"])
    conn = await session.scalar(select(StoreConnection).where(StoreConnection.tenant_id == tenant_id))
    if conn is None:
        raise PermanentJobError("store not connected")
    try:
        count = await catalog.sync_catalog(session, tenant_id, build_connector(conn))
    except StoreError as exc:
        conn.last_error = str(exc)
        if exc.permanent:
            raise PermanentJobError(str(exc)) from exc
        raise
    conn.last_synced_at = datetime.now(timezone.utc)
    conn.last_error = None
    log.info("synced %s products for tenant %s", count, tenant_id)


# --- bulk campaigns ---


@handler("campaign_send")
async def handle_campaign_send(session: AsyncSession, job: Job) -> None:
    campaign_id = job.payload["campaign_id"]
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None or campaign.status != "sending":
        return  # cancelled or finished
    tenant = await session.get(Tenant, campaign.tenant_id)
    assert tenant is not None
    sender = await tenant_sender(session, tenant)
    result = await campaign_service.send_batch(session, sender, tenant, campaign)
    campaign = await session.get(Campaign, campaign_id, populate_existing=True)
    assert campaign is not None
    if result.paused_reason:
        campaign.status = "paused"
        campaign.pause_reason = result.paused_reason
    elif result.rate_limited:
        await enqueue(session, "campaign_send", {"campaign_id": campaign_id}, tenant_id=tenant.id, delay_seconds=60)
    elif result.remaining and campaign.status == "sending":
        await enqueue(session, "campaign_send", {"campaign_id": campaign_id}, tenant_id=tenant.id, delay_seconds=1)
    elif campaign.status == "sending":
        campaign.status = "done"
        campaign.finished_at = datetime.now(timezone.utc)
