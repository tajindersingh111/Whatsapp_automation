"""Parse Meta webhook payloads into flat, typed events."""

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class InboundMessage:
    phone_number_id: str
    wa_id: str
    wa_message_id: str
    timestamp: datetime
    kind: str
    text: str
    profile_name: str | None = None
    # Reply-button id (interactive) or template quick-reply payload.
    button_payload: str | None = None
    # Click-to-WhatsApp ad referral, if the chat started from an ad.
    referral: dict | None = None
    context_message_id: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class StatusUpdate:
    phone_number_id: str
    wa_message_id: str
    status: str  # sent | delivered | read | failed
    error: str | None = None


def _ts(value: str | None) -> datetime:
    try:
        return datetime.fromtimestamp(int(value or 0), tz=timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def _message_text(msg: dict) -> tuple[str, str, str | None]:
    """Returns (kind, text, button_payload)."""
    kind = msg.get("type", "unknown")
    if kind == "text":
        return kind, msg.get("text", {}).get("body", ""), None
    if kind == "interactive":
        inter = msg.get("interactive", {})
        reply = inter.get("button_reply") or inter.get("list_reply") or {}
        return "button", reply.get("title", ""), reply.get("id")
    if kind == "button":  # quick reply on a template
        btn = msg.get("button", {})
        return "button", btn.get("text", ""), btn.get("payload")
    if kind in ("image", "video", "document", "audio", "sticker"):
        media = msg.get(kind, {})
        caption = media.get("caption", "")
        return kind, caption or f"[customer sent a {kind}]", None
    if kind == "location":
        loc = msg.get("location", {})
        return kind, f"[location: {loc.get('name') or ''} {loc.get('address') or ''} ({loc.get('latitude')},{loc.get('longitude')})]", None
    if kind == "order":  # WhatsApp catalog cart
        order = msg.get("order", {})
        items = ", ".join(
            f"{i.get('quantity')} x {i.get('product_retailer_id')}" for i in order.get("product_items", [])
        )
        return kind, f"[cart from WhatsApp catalog: {items}] {order.get('text', '')}".strip(), None
    if kind == "reaction":
        return kind, f"[reacted {msg.get('reaction', {}).get('emoji', '')}]", None
    return kind, f"[unsupported message type: {kind}]", None


def parse_webhook(payload: dict) -> tuple[list[InboundMessage], list[StatusUpdate]]:
    messages: list[InboundMessage] = []
    statuses: list[StatusUpdate] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") != "messages":
                continue
            value = change.get("value", {})
            phone_number_id = value.get("metadata", {}).get("phone_number_id", "")
            names = {c.get("wa_id"): c.get("profile", {}).get("name") for c in value.get("contacts", [])}
            for msg in value.get("messages", []):
                kind, text, payload_id = _message_text(msg)
                messages.append(
                    InboundMessage(
                        phone_number_id=phone_number_id,
                        wa_id=msg.get("from", ""),
                        wa_message_id=msg.get("id", ""),
                        timestamp=_ts(msg.get("timestamp")),
                        kind=kind,
                        text=text,
                        profile_name=names.get(msg.get("from")),
                        button_payload=payload_id,
                        referral=msg.get("referral"),
                        context_message_id=(msg.get("context") or {}).get("id"),
                        raw=msg,
                    )
                )
            for st in value.get("statuses", []):
                errors = st.get("errors") or []
                err = None
                if errors:
                    e = errors[0]
                    err = f"{e.get('code')}: {e.get('title')} {e.get('error_data', {}).get('details', '')}".strip()
                statuses.append(
                    StatusUpdate(
                        phone_number_id=phone_number_id,
                        wa_message_id=st.get("id", ""),
                        status=st.get("status", ""),
                        error=err,
                    )
                )
    return messages, statuses
