"""Contacts, conversations and the message log (the inbox)."""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LEAD_STAGES, Contact, Conversation, Message

SERVICE_WINDOW = timedelta(hours=24)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def get_or_create_contact(
    session: AsyncSession, tenant_id: uuid.UUID, wa_id: str, name: str | None = None, source: str | None = None
) -> Contact:
    stmt = (
        insert(Contact)
        .values(tenant_id=tenant_id, wa_id=wa_id, name=name, source=source, lead_stage="new")
        .on_conflict_do_nothing(index_elements=["tenant_id", "wa_id"])
    )
    await session.execute(stmt)
    contact = await session.scalar(select(Contact).where(Contact.tenant_id == tenant_id, Contact.wa_id == wa_id))
    assert contact is not None
    if name and not contact.name:
        contact.name = name
    if source and not contact.source:
        contact.source = source
    return contact


async def get_or_create_conversation(
    session: AsyncSession, tenant_id: uuid.UUID, contact: Contact, is_playground: bool = False
) -> Conversation:
    stmt = (
        insert(Conversation)
        .values(tenant_id=tenant_id, contact_id=contact.id, is_playground=is_playground, mode="bot")
        .on_conflict_do_nothing(index_elements=["tenant_id", "contact_id"])
    )
    await session.execute(stmt)
    conv = await session.scalar(
        select(Conversation).where(Conversation.tenant_id == tenant_id, Conversation.contact_id == contact.id)
    )
    assert conv is not None
    return conv


async def record_message(
    session: AsyncSession,
    conv: Conversation,
    *,
    direction: str,
    sender: str,
    body: str,
    kind: str = "text",
    payload: dict | None = None,
    wa_message_id: str | None = None,
    status: str = "received",
    error: str | None = None,
    created_at: datetime | None = None,
) -> Message:
    msg = Message(
        tenant_id=conv.tenant_id,
        conversation_id=conv.id,
        direction=direction,
        sender=sender,
        kind=kind,
        body=body,
        payload=payload or {},
        wa_message_id=wa_message_id,
        status=status,
        error=error,
    )
    if created_at:
        msg.created_at = created_at
    session.add(msg)
    conv.last_message_at = created_at or utcnow()
    await session.flush()
    return msg


def in_service_window(contact: Contact) -> bool:
    """Free-form messages are only allowed within 24h of the customer's last message."""
    return contact.last_inbound_at is not None and utcnow() - contact.last_inbound_at < SERVICE_WINDOW


async def recent_messages(session: AsyncSession, conversation_id: int, limit: int = 30) -> list[Message]:
    rows = await session.scalars(
        select(Message).where(Message.conversation_id == conversation_id).order_by(Message.id.desc()).limit(limit)
    )
    return list(reversed(rows.all()))


async def latest_inbound(session: AsyncSession, conversation_id: int) -> Message | None:
    return await session.scalar(
        select(Message)
        .where(Message.conversation_id == conversation_id, Message.direction == "in")
        .order_by(Message.id.desc())
        .limit(1)
    )


def to_llm_history(messages: list[Message]) -> tuple[list[dict], list[str]]:
    """Turn the message log into alternating user/assistant turns.

    Returns (messages, preamble). Outbound messages sent before the customer's first reply
    (e.g. a COD confirmation template) cannot open an API conversation, so they are returned
    separately as preamble text for the system context.
    """
    turns: list[dict] = []
    preamble: list[str] = []
    for m in messages:
        role = "user" if m.direction == "in" else "assistant"
        text = m.body.strip() or f"[{m.kind}]"
        if m.direction == "out" and m.sender == "human":
            text = f"(sent by a human team member) {text}"
        if not turns and role == "assistant":
            preamble.append(text)
            continue
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"] += "\n" + text
        else:
            turns.append({"role": role, "content": text})
    return turns, preamble


def set_lead_stage(contact: Contact, stage: str, note: str | None = None) -> None:
    """Move a lead forward (never back from 'ordered'); 'lost' is always allowed."""
    rank = {s: i for i, s in enumerate(LEAD_STAGES)}
    if note:
        contact.lead_note = note
    if contact.lead_stage == "ordered" and stage != "ordered":
        return
    if stage == "lost" or rank.get(stage, 0) >= rank.get(contact.lead_stage, 0):
        contact.lead_stage = stage
