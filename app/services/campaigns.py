"""Bulk sends ("campaigns"): one approved template to a CSV list of opted-in customers.

Meta rules this module follows:
- Business-initiated messages must be approved templates (no free text).
- Customers who replied STOP are skipped.
- Sends are throttled; Meta rate-limit errors pause the campaign and retry later.
"""

import asyncio
import csv
import io
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import normalize_phone
from app.models import Campaign, CampaignRecipient, Contact, Tenant
from app.services.conversations import get_or_create_contact, get_or_create_conversation
from app.services.messaging import send_message
from app.whatsapp.client import RATE_LIMIT_CODES, Sender, WhatsAppError, template_message

log = logging.getLogger(__name__)

MAX_RECIPIENTS = 10_000
MAX_CSV_BYTES = 5 * 1024 * 1024
BATCH_SIZE = 50
REPLY_WINDOW = timedelta(days=7)
SEND_INTERVAL_SECONDS = 0.1  # ~10 messages/second, well under Cloud API throughput

_PHONE_HEADERS = ("phone", "mobile", "number", "whatsapp", "contact", "phone number", "mobile number", "whatsapp number", "phone_number", "mobile_no", "phone no", "mob")
_NAME_HEADERS = ("name", "customer name", "full name", "customer", "first name", "first_name")


@dataclass
class ParsedCsv:
    columns: list[str]
    rows: list[dict]  # {"phone": "...", "name": "...", "vars": {...}}
    invalid: list[str] = field(default_factory=list)
    duplicates: int = 0


def valid_phone(digits: str) -> bool:
    if digits.startswith("91"):
        return len(digits) == 12 and digits[2] in "6789"
    return 10 <= len(digits) <= 15


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_csv(raw: bytes) -> ParsedCsv:
    """Read a customer list. A header row is optional; without one, column 1 = phone, column 2 = name."""
    if len(raw) > MAX_CSV_BYTES:
        raise ValueError("CSV is larger than 5 MB")
    text = _decode(raw)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    table = [row for row in csv.reader(io.StringIO(text), dialect) if any(cell.strip() for cell in row)]
    if not table:
        raise ValueError("CSV is empty")

    first = [c.strip() for c in table[0]]
    has_header = not any(len(re.sub(r"\D", "", c)) >= 10 for c in first)
    if has_header:
        columns = [c or f"column{i + 1}" for i, c in enumerate(first)]
        body = table[1:]
    else:
        columns = ["phone", "name"] + [f"column{i + 1}" for i in range(2, max(len(r) for r in table))]
        body = table
    lowered = [c.lower() for c in columns]
    phone_idx = next((lowered.index(h) for h in _PHONE_HEADERS if h in lowered), None)
    if phone_idx is None:
        # Fall back to the first column that mostly contains phone-like values.
        for i in range(len(columns)):
            sample = [r[i] for r in body[:20] if i < len(r)]
            if sample and sum(len(re.sub(r"\D", "", v)) >= 10 for v in sample) >= len(sample) / 2:
                phone_idx = i
                break
    if phone_idx is None:
        raise ValueError("Could not find a phone number column (name it 'phone')")
    name_idx = next((lowered.index(h) for h in _NAME_HEADERS if h in lowered), None)

    parsed = ParsedCsv(columns=columns, rows=[])
    seen: set[str] = set()
    for row in body:
        cells = [c.strip() for c in row] + [""] * (len(columns) - len(row))
        raw_phone = cells[phone_idx] if phone_idx < len(cells) else ""
        digits = normalize_phone(raw_phone)
        if not valid_phone(digits):
            parsed.invalid.append(raw_phone or "(empty)")
            continue
        if digits in seen:
            parsed.duplicates += 1
            continue
        seen.add(digits)
        values = {columns[i]: cells[i] for i in range(len(columns))}
        name = cells[name_idx] if name_idx is not None and name_idx < len(cells) else ""
        parsed.rows.append({"phone": digits, "name": name or None, "vars": values})
        if len(parsed.rows) > MAX_RECIPIENTS:
            raise ValueError(f"More than {MAX_RECIPIENTS:,} numbers; split the list into smaller campaigns")
    return parsed


def resolve_params(params: list[dict], name: str | None, values: dict) -> list[str]:
    """Body parameters for one recipient. Meta rejects empty parameters, so a fallback is always used."""
    out = []
    for spec in params:
        if spec.get("source") == "column":
            column = spec.get("value", "")
            value = (values.get(column) or "").strip()
            if not value and column.lower() in _NAME_HEADERS:
                value = (name or "").strip()
        else:
            value = (spec.get("value") or "").strip()
        out.append(value or (spec.get("fallback") or "").strip() or "-")
    return out


@dataclass
class Skipped:
    opted_out: int = 0
    excluded: int = 0
    never_delivered: int = 0
    never_replied: int = 0

    @property
    def total(self) -> int:
        return self.opted_out + self.excluded + self.never_delivered + self.never_replied


@dataclass
class Engagement:
    """One customer's history across all campaigns of a brand."""

    attempts: int = 0  # messages we tried to send
    delivered: int = 0  # reached the phone (2 grey ticks or better)
    read: int = 0  # blue ticks (only if the customer has read receipts on)
    replied: int = 0  # customer wrote back after the message
    failed: int = 0  # Meta could not deliver at all
    last_sent_at: datetime | None = None

    @property
    def segment(self) -> str:
        if self.replied:
            return "replied"
        if self.read:
            return "read_no_reply"
        if self.delivered:
            return "delivered_unread"
        if self.attempts and self.failed == self.attempts:
            return "failed"
        if self.attempts:
            return "not_delivered"
        return "none"


SEGMENTS = {
    "replied": "Replied",
    "read_no_reply": "Seen, no reply",
    "delivered_unread": "Delivered, not seen",
    "not_delivered": "Single tick (not delivered)",
    "failed": "Failed",
}

# Auto-skip rules applied when a new campaign is created.
NEVER_DELIVERED_AFTER = 2  # 2+ campaign messages, none ever reached the phone
NEVER_REPLIED_AFTER = 3  # 3+ campaign messages delivered, never a reply


async def engagement_by_phone(session: AsyncSession, tenant_id, phones: list[str] | None = None) -> dict[str, Engagement]:
    r = CampaignRecipient
    attempted = r.status.in_(["sent", "delivered", "read", "failed"])
    stmt = (
        select(
            r.phone,
            func.count().filter(attempted),
            func.count().filter(r.status.in_(["delivered", "read"]) | r.replied_at.is_not(None)),
            func.count().filter((r.status == "read") | r.replied_at.is_not(None)),
            func.count().filter(r.replied_at.is_not(None)),
            func.count().filter(r.status == "failed"),
            func.max(r.sent_at),
        )
        .where(r.tenant_id == tenant_id)
        .group_by(r.phone)
    )
    if phones is not None:
        stmt = stmt.where(r.phone.in_(phones))
    return {
        phone: Engagement(attempts=a, delivered=d, read=rd, replied=rp, failed=f, last_sent_at=last)
        for phone, a, d, rd, rp, f, last in (await session.execute(stmt)).all()
    }


async def mark_replied(session: AsyncSession, tenant_id, phone: str, at: datetime) -> None:
    """A customer message counts as a reply to campaign messages sent to them in the last 7 days."""
    r = CampaignRecipient
    await session.execute(
        update(r)
        .where(
            r.tenant_id == tenant_id, r.phone == phone, r.replied_at.is_(None),
            r.status.in_(["sent", "delivered", "read"]), r.sent_at >= at - REPLY_WINDOW, r.sent_at <= at,
        )
        .values(replied_at=at, status="read")
    )


async def add_recipients(
    session: AsyncSession, campaign: Campaign, rows: list[dict], *, skip_never_delivered: bool = True, skip_never_replied: bool = False
) -> tuple[int, Skipped]:
    """Insert recipients. Opted-out, excluded and (optionally) unengaged customers are stored as skipped."""
    phones = [r["phone"] for r in rows]
    contacts = {
        c.wa_id: c for c in (await session.scalars(
            select(Contact).where(
                Contact.tenant_id == campaign.tenant_id, Contact.wa_id.in_(phones),
                Contact.opted_out.is_(True) | Contact.campaigns_excluded.is_(True),
            )
        )).all()
    }
    history = await engagement_by_phone(session, campaign.tenant_id, phones) if (skip_never_delivered or skip_never_replied) else {}
    skipped = Skipped()

    def reason(phone: str) -> str | None:
        c = contacts.get(phone)
        if c is not None and c.opted_out:
            skipped.opted_out += 1
            return "customer opted out (STOP)"
        if c is not None and c.campaigns_excluded:
            skipped.excluded += 1
            return "excluded from campaigns" + (f": {c.excluded_reason}" if c.excluded_reason else "")
        h = history.get(phone)
        if h and skip_never_delivered and h.attempts >= NEVER_DELIVERED_AFTER and h.delivered == 0:
            skipped.never_delivered += 1
            return f"auto-skipped: last {h.attempts} campaign messages were never delivered"
        if h and skip_never_replied and h.delivered >= NEVER_REPLIED_AFTER and h.replied == 0:
            skipped.never_replied += 1
            return f"auto-skipped: {h.delivered} campaign messages delivered, never replied"
        return None

    for start in range(0, len(rows), 1000):
        values = []
        for r in rows[start:start + 1000]:
            why = reason(r["phone"])
            values.append({
                "campaign_id": campaign.id, "tenant_id": campaign.tenant_id, "phone": r["phone"], "name": r["name"],
                "vars": r["vars"], "status": "skipped" if why else "pending", "error": why,
            })
        await session.execute(insert(CampaignRecipient).values(values).on_conflict_do_nothing(index_elements=["campaign_id", "phone"]))
    campaign.total = await session.scalar(
        select(func.count()).select_from(CampaignRecipient).where(CampaignRecipient.campaign_id == campaign.id)
    ) or 0
    return len(rows), skipped


def build_message(campaign: Campaign, name: str | None, values: dict) -> dict:
    return template_message(
        campaign.template_name,
        campaign.template_language,
        body_params=resolve_params(campaign.params or [], name, values) or None,
        header_image_url=campaign.header_image_url,
    )


async def send_to(session: AsyncSession, sender: Sender, tenant: Tenant, campaign: Campaign, phone: str, name: str | None, values: dict):
    """Send the campaign template to one number and log it in that customer's conversation."""
    contact = await get_or_create_contact(session, tenant.id, phone, name, source=f"campaign: {campaign.name}"[:100])
    conv = await get_or_create_conversation(session, tenant.id, contact)
    return contact, await send_message(session, sender, conv, contact, build_message(campaign, name, values), by="campaign")


@dataclass
class BatchResult:
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    remaining: int = 0
    rate_limited: bool = False


async def send_batch(session: AsyncSession, sender: Sender, tenant: Tenant, campaign: Campaign, limit: int = BATCH_SIZE) -> BatchResult:
    result = BatchResult()
    campaign_id = campaign.id  # plain ids: a rollback expires ORM objects
    recipients = (await session.scalars(
        select(CampaignRecipient)
        .where(CampaignRecipient.campaign_id == campaign.id, CampaignRecipient.status == "pending")
        .order_by(CampaignRecipient.id)
        .limit(limit)
    )).all()
    for r in recipients:
        contact = await session.scalar(select(Contact).where(Contact.tenant_id == tenant.id, Contact.wa_id == r.phone))
        if contact is not None and (contact.opted_out or contact.campaigns_excluded):
            r.status, r.error = "skipped", "customer opted out (STOP)" if contact.opted_out else "excluded from campaigns"
            result.skipped += 1
            await session.commit()
            continue
        try:
            _, msg = await send_to(session, sender, tenant, campaign, r.phone, r.name, r.vars or {})
        except WhatsAppError as exc:
            # Permanent errors come back as a failed message; anything raised here is temporary
            # (rate limit, Meta outage): pause and resume later without touching this recipient.
            await session.rollback()
            log.warning("campaign %s paused (%s): %s", campaign_id, "rate limit" if exc.code in RATE_LIMIT_CODES else "temporary error", exc)
            result.rate_limited = True
            break
        r.sent_at = datetime.now(timezone.utc)
        if msg.status == "failed":
            r.status, r.error = "failed", msg.error
            result.failed += 1
        else:
            r.status, r.wa_message_id = "sent", msg.wa_message_id
            result.sent += 1
        # Commit per message: a crash later in the batch must never cause a second send to this customer.
        await session.commit()
        if not sender.simulated:
            await asyncio.sleep(SEND_INTERVAL_SECONDS)
    result.remaining = await session.scalar(
        select(func.count()).select_from(CampaignRecipient)
        .where(CampaignRecipient.campaign_id == campaign_id, CampaignRecipient.status == "pending")
    ) or 0
    return result


async def counts(session: AsyncSession, campaign_id: int) -> dict[str, int]:
    rows = (await session.execute(
        select(CampaignRecipient.status, func.count()).where(CampaignRecipient.campaign_id == campaign_id).group_by(CampaignRecipient.status)
    )).all()
    out = {s: 0 for s in ("pending", "sent", "delivered", "read", "failed", "skipped")}
    out.update({status: n for status, n in rows})
    out["replied"] = await session.scalar(
        select(func.count()).select_from(CampaignRecipient)
        .where(CampaignRecipient.campaign_id == campaign_id, CampaignRecipient.replied_at.is_not(None))
    ) or 0
    return out
