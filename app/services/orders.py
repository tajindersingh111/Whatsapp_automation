"""Order mirror + the COD confirmation and shipping-update automations."""

import logging
import uuid

from sqlalchemy import func, literal_column, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot_config import BotConfig, load_config
from app.connectors.base import OrderData, StoreConnector, StoreError
from app.jobs import PermanentJobError, enqueue
from app.models import Contact, Conversation, Order, Tenant
from app.services.conversations import get_or_create_contact, get_or_create_conversation
from app.whatsapp.client import Sender, template_message, text_message

log = logging.getLogger(__name__)

COD_CONFIRM = "cod:confirm:"
COD_CANCEL = "cod:cancel:"


async def upsert_order(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    data: OrderData,
    *,
    contact_id: int | None = None,
    created_via: str = "store",
) -> tuple[Order, bool]:
    """Insert or refresh the local copy. Returns (order, created)."""
    values = {
        "number": data.number,
        "status": data.status,
        "payment_method": data.payment_method,
        "total": data.total,
        "currency": data.currency,
        "customer_name": data.customer_name,
        "customer_phone": data.customer_phone,
        "items": data.items,
        "tracking": data.tracking,
    }
    stmt = (
        insert(Order)
        .values(tenant_id=tenant_id, external_id=data.external_id, contact_id=contact_id, created_via=created_via, **values)
        .on_conflict_do_update(index_elements=["tenant_id", "external_id"], set_={**values, "updated_at": func.now()})
        .returning(Order.id, literal_column("(xmax = 0)").label("created"))
    )
    row = (await session.execute(stmt)).one()
    order = await session.get(Order, row.id, populate_existing=True)
    assert order is not None
    if contact_id and order.contact_id is None:
        order.contact_id = contact_id
    return order, bool(row.created)


def _first_name(name: str | None) -> str:
    return (name or "").split(" ")[0] or "there"


async def _contact_for_order(session: AsyncSession, tenant: Tenant, order: Order) -> tuple[Contact, Conversation] | None:
    if order.contact_id:
        contact = await session.get(Contact, order.contact_id)
    elif order.customer_phone:
        contact = await get_or_create_contact(session, tenant.id, order.customer_phone, order.customer_name, source="store order")
        order.contact_id = contact.id
    else:
        return None
    if contact is None:
        return None
    conv = await get_or_create_conversation(session, tenant.id, contact)
    return contact, conv


async def start_cod_confirmation(session: AsyncSession, tenant: Tenant, order: Order) -> None:
    """Called when the store reports a new COD order."""
    cfg = load_config(tenant.config)
    flow = cfg.cod_confirmation
    if not flow.enabled or order.cod_status is not None or order.payment_method != "cod" or order.status == "cancelled":
        return
    order.cod_status = "pending"
    await enqueue(session, "cod_send_confirmation", {"order_id": order.id}, tenant_id=tenant.id, dedupe_key=f"cod-confirm:{order.id}")


async def send_cod_template(session: AsyncSession, sender: Sender, tenant: Tenant, order: Order) -> None:
    from app.services.messaging import send_message

    cfg = load_config(tenant.config)
    found = await _contact_for_order(session, tenant, order)
    if found is None:
        order.cod_status = "no_phone"
        return
    contact, conv = found
    flow = cfg.cod_confirmation
    message = template_message(
        flow.template_name,
        flow.template_language,
        body_params=[_first_name(order.customer_name or contact.name), order.number, f"₹{order.total:,.0f}"],
        quick_reply_payloads=[f"{COD_CONFIRM}{order.id}", f"{COD_CANCEL}{order.id}"],
    )
    await send_message(session, sender, conv, contact, message, by="flow")


async def handle_cod_reply(
    session: AsyncSession,
    tenant: Tenant,
    cfg: BotConfig,
    connector: StoreConnector | None,
    sender: Sender,
    contact: Contact,
    conv: Conversation,
    payload: str,
) -> bool:
    """Handle a tap on the template's Confirm/Cancel button. Returns True if handled."""
    from app.services.messaging import send_message

    if payload.startswith(COD_CONFIRM):
        action, raw_id = "confirm", payload.removeprefix(COD_CONFIRM)
    elif payload.startswith(COD_CANCEL):
        action, raw_id = "cancel", payload.removeprefix(COD_CANCEL)
    else:
        return False
    order = await session.get(Order, int(raw_id)) if raw_id.isdigit() else None
    if order is None or order.tenant_id != tenant.id or order.contact_id != contact.id:
        await send_message(session, sender, conv, contact, text_message("Sorry, we couldn't find that order. Our team will check and get back to you."), by="flow")
        return True
    if order.cod_status in ("confirmed", "cancelled"):
        reply = f"Order {order.number} is already {order.cod_status}. Need help with anything else?"
        await send_message(session, sender, conv, contact, text_message(reply), by="flow")
        return True
    flow = cfg.cod_confirmation
    try:
        if connector is not None:
            if action == "confirm":
                await connector.confirm_cod_order(order.external_id)
            else:
                await connector.cancel_order(order.external_id, "Customer cancelled COD order on WhatsApp")
    except StoreError as exc:
        if not exc.permanent:
            raise
        log.error("store rejected COD %s for order %s: %s", action, order.id, exc)
        conv.mode = "human"
        conv.handoff_reason = f"Could not {action} order {order.number} in the store: {exc}"
        await send_message(session, sender, conv, contact, text_message("Thanks! Our team will update your order shortly."), by="flow")
        return True
    if action == "confirm":
        order.cod_status = "confirmed"
        reply = flow.confirmed_reply.format(number=order.number)
    else:
        order.cod_status = "cancelled"
        order.status = "cancelled"
        reply = flow.cancelled_reply.format(number=order.number)
    await send_message(session, sender, conv, contact, text_message(reply), by="flow")
    return True


async def maybe_notify_shipping(session: AsyncSession, tenant: Tenant, order: Order) -> None:
    cfg = load_config(tenant.config)
    tracking = order.tracking or {}
    marker = tracking.get("awb") or tracking.get("url")
    if not cfg.shipping_update.enabled or not marker or order.shipping_notified_status == marker or order.status == "cancelled":
        return
    order.shipping_notified_status = marker
    await enqueue(session, "shipping_update", {"order_id": order.id}, tenant_id=tenant.id, dedupe_key=f"ship:{order.id}:{marker}"[:200])


async def send_shipping_template(session: AsyncSession, sender: Sender, tenant: Tenant, order: Order) -> None:
    from app.services.messaging import send_message

    cfg = load_config(tenant.config)
    found = await _contact_for_order(session, tenant, order)
    if found is None:
        return
    contact, conv = found
    tracking = order.tracking or {}
    flow = cfg.shipping_update
    message = template_message(
        flow.template_name,
        flow.template_language,
        body_params=[
            _first_name(order.customer_name or contact.name),
            order.number,
            tracking.get("courier") or "our courier partner",
            tracking.get("url") or f"AWB {tracking.get('awb')}",
        ],
    )
    await send_message(session, sender, conv, contact, message, by="flow")


async def find_order(session: AsyncSession, order_id: int) -> Order:
    order = await session.get(Order, order_id)
    if order is None:
        raise PermanentJobError(f"order {order_id} not found")
    return order


async def orders_for_contact(session: AsyncSession, tenant_id: uuid.UUID, contact_id: int) -> list[Order]:
    rows = await session.scalars(
        select(Order).where(Order.tenant_id == tenant_id, Order.contact_id == contact_id).order_by(Order.id.desc()).limit(5)
    )
    return list(rows.all())


def order_view(o) -> dict:
    """Customer-safe summary of an Order row or OrderData."""
    view = {
        "order_number": o.number,
        "status": o.status,
        "payment": "Cash on Delivery" if o.payment_method == "cod" else "Prepaid",
        "total": f"₹{o.total:,.0f}",
        "items": [f"{i.get('quantity')} x {i.get('title')}" for i in o.items][:10],
        "placed_at": o.created_at.isoformat() if hasattr(o.created_at, "isoformat") else o.created_at,
    }
    if o.tracking:
        view["tracking"] = {k: v for k, v in o.tracking.items() if v}
    return view


async def lookup_customer_orders(
    session: AsyncSession, tenant_id: uuid.UUID, connector: StoreConnector | None, contact: Contact, order_number: str | None
) -> tuple[list, str | None]:
    """Orders this customer may see: by number (only if it belongs to their phone) or their recent ones."""
    phone = contact.wa_id
    if connector is not None:
        try:
            if order_number:
                order = await connector.get_order(order_number)
                if order is None:
                    return [], f"No order found with number {order_number}."
                if order.customer_phone and order.customer_phone[-10:] != phone[-10:]:
                    return [], "That order is registered to a different phone number; ask the customer to message from that number or hand over to a human."
                orders = [order]
            else:
                orders = (await connector.find_orders_by_phone(phone))[:3]
            for o in orders:
                await upsert_order(session, tenant_id, o, contact_id=contact.id)
            return orders, None
        except StoreError as exc:
            log.warning("order lookup failed, using local copy: %s", exc)
    # Fall back to the local mirror (orders seen via webhooks).
    stmt = select(Order).where(Order.tenant_id == tenant_id)
    if order_number:
        bare = order_number.lstrip("#")
        stmt = stmt.where(Order.number.in_([order_number, bare, f"#{bare}"]))
    stmt = stmt.where(or_(Order.contact_id == contact.id, Order.customer_phone == phone)).order_by(Order.id.desc()).limit(3)
    return list((await session.scalars(stmt)).all()), None
