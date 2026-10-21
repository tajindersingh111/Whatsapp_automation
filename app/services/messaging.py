"""Outbound messages: pick the right sender, respect the 24h window, log everything."""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.connectors import http_client
from app.jobs import PermanentJobError
from app.models import Contact, Conversation, Message, Tenant, WhatsAppAccount
from app.security import decrypt
from app.services.conversations import in_service_window, record_message
from app.whatsapp.client import CloudApiSender, Sender, SimulatedSender, WhatsAppError, describe

log = logging.getLogger(__name__)


class OutsideServiceWindow(Exception):
    pass


async def sender_for(session: AsyncSession, tenant: Tenant, conv: Conversation) -> Sender:
    if conv.is_playground:
        return SimulatedSender()
    return await tenant_sender(session, tenant)


async def tenant_sender(session: AsyncSession, tenant: Tenant) -> Sender:
    account = await session.scalar(select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == tenant.id))
    # "sandbox-*" numbers are for demos: messages are logged in the inbox but never leave the server.
    if account is None or account.phone_number_id.startswith("sandbox"):
        return SimulatedSender()
    settings = get_settings()
    return CloudApiSender(account.phone_number_id, decrypt(account.access_token_enc), settings.meta_graph_version, http_client())


async def send_message(
    session: AsyncSession,
    sender: Sender,
    conv: Conversation,
    contact: Contact,
    message: dict,
    *,
    by: str,
) -> Message:
    """Send and log one message. `by` is bot | human | flow | system."""
    if contact.opted_out and by != "system":
        raise PermanentJobError("Contact opted out of messages")
    if message.get("type") != "template" and not conv.is_playground and not sender.simulated and not in_service_window(contact):
        raise OutsideServiceWindow("More than 24h since the customer's last message; use an approved template")
    body = describe(message)
    try:
        wamid = await sender.send(contact.wa_id, message)
    except WhatsAppError as exc:
        await record_message(
            session, conv, direction="out", sender=by, body=body, kind=message["type"], payload=message, status="failed", error=str(exc)
        )
        if exc.permanent:
            log.warning("permanent WhatsApp error for conversation %s: %s", conv.id, exc)
            return await _last_out(session, conv)
        raise
    return await record_message(
        session,
        conv,
        direction="out",
        sender=by,
        body=body,
        kind=message["type"],
        payload=message,
        wa_message_id=wamid,
        status="simulated" if sender.simulated else "sent",
    )


async def _last_out(session: AsyncSession, conv: Conversation) -> Message:
    msg = await session.scalar(
        select(Message).where(Message.conversation_id == conv.id, Message.direction == "out").order_by(Message.id.desc()).limit(1)
    )
    assert msg is not None
    return msg
