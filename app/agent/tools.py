"""Tools the sales agent can call. Every fact the bot states about products/orders comes from here."""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot_config import BotConfig
from app.connectors.base import CustomerInput, OrderItemInput, StoreConnector, StoreError, normalize_phone
from app.models import Contact, Conversation, KnowledgeDoc, Order, Tenant
from app.services import catalog
from app.services.messaging import send_message
from app.services.conversations import set_lead_stage
from app.services.orders import lookup_customer_orders, order_view, upsert_order
from app.whatsapp.client import Sender, WhatsAppError, image_message

log = logging.getLogger(__name__)


@dataclass
class ToolContext:
    session: AsyncSession
    tenant: Tenant
    cfg: BotConfig
    contact: Contact
    conv: Conversation
    sender: Sender
    connector: StoreConnector | None
    # Playground: never create real orders in the store.
    dry_run: bool = False
    knowledge_inline: bool = True
    events: list[dict] = field(default_factory=list)


# --- input models ---


class SearchProductsInput(BaseModel):
    query: str = Field(default="", max_length=200)
    max_price: float | None = None
    limit: int = Field(default=5, ge=1, le=10)


class ProductIdInput(BaseModel):
    product_id: str


class SendImagesInput(BaseModel):
    product_ids: list[str] = Field(min_length=1, max_length=3)


class Item(BaseModel):
    product_id: str
    variant_id: str | None = None
    quantity: int = Field(default=1, ge=1, le=20)


class CheckoutInput(BaseModel):
    items: list[Item] = Field(min_length=1, max_length=10)


class CustomerAddress(BaseModel):
    name: str = Field(min_length=2)
    address_line1: str = Field(min_length=5)
    address_line2: str = ""
    city: str = Field(min_length=2)
    state: str = Field(min_length=2)
    pincode: str
    phone: str | None = None
    email: str | None = None


class PlaceCodInput(BaseModel):
    customer: CustomerAddress
    items: list[Item] = Field(min_length=1, max_length=10)
    customer_confirmed: bool


class TrackOrderInput(BaseModel):
    order_number: str | None = None


class UpdateLeadInput(BaseModel):
    stage: Literal["browsing", "interested", "checkout_sent", "ordered", "lost"]
    note: str = Field(default="", max_length=500)


class HandoffInput(BaseModel):
    reason: str = Field(max_length=500)


class SearchKnowledgeInput(BaseModel):
    query: str = Field(max_length=200)


_item_schema = {
    "type": "object",
    "properties": {
        "product_id": {"type": "string"},
        "variant_id": {"type": ["string", "null"], "description": "Required when the product has variants (size/colour)."},
        "quantity": {"type": "integer", "minimum": 1},
    },
    "required": ["product_id", "quantity"],
}

TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "search_products",
        "description": "Search the store's live catalog. Returns matching products with product_id, price, MRP, stock, variants and URL. Use the customer's words (type, colour, material, occasion). Empty query returns popular in-stock items.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keywords, e.g. 'blue cotton bedsheet king size'"},
                "max_price": {"type": ["number", "null"], "description": "Budget ceiling in rupees, if the customer gave one."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_product_details",
        "description": "Full details for one product: description, variants with ids, stock, tags.",
        "input_schema": {"type": "object", "properties": {"product_id": {"type": "string"}}, "required": ["product_id"]},
    },
    {
        "name": "send_product_images",
        "description": "Send product photos to the customer on WhatsApp, each with title, price and link as caption. Max 3 per call.",
        "input_schema": {
            "type": "object",
            "properties": {"product_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 3}},
            "required": ["product_ids"],
        },
    },
    {
        "name": "create_checkout_link",
        "description": "Create a checkout/cart link with these items so the customer can pay online. Include the returned URL in your reply.",
        "input_schema": {
            "type": "object",
            "properties": {"items": {"type": "array", "items": _item_schema, "minItems": 1}},
            "required": ["items"],
        },
    },
    {
        "name": "place_cod_order",
        "description": "Place a Cash on Delivery order in the store. Only call after you sent the customer a summary (items, total, address) and they clearly confirmed it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "customer": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "address_line1": {"type": "string", "description": "House/flat, street"},
                        "address_line2": {"type": "string", "description": "Area, landmark"},
                        "city": {"type": "string"},
                        "state": {"type": "string"},
                        "pincode": {"type": "string", "description": "6 digits"},
                        "phone": {"type": ["string", "null"], "description": "Only if different from their WhatsApp number"},
                        "email": {"type": ["string", "null"]},
                    },
                    "required": ["name", "address_line1", "city", "state", "pincode"],
                },
                "items": {"type": "array", "items": _item_schema, "minItems": 1},
                "customer_confirmed": {"type": "boolean", "description": "True only if the customer confirmed the order summary."},
            },
            "required": ["customer", "items", "customer_confirmed"],
        },
    },
    {
        "name": "track_order",
        "description": "Get status and tracking of this customer's orders. Pass the order number if they gave one; otherwise their recent orders are looked up by their WhatsApp number.",
        "input_schema": {
            "type": "object",
            "properties": {"order_number": {"type": ["string", "null"]}},
            "required": [],
        },
    },
    {
        "name": "update_lead",
        "description": "Record the customer's buying stage and a short note of what they want (for the brand's sales team).",
        "input_schema": {
            "type": "object",
            "properties": {
                "stage": {"type": "string", "enum": ["browsing", "interested", "checkout_sent", "ordered", "lost"]},
                "note": {"type": "string", "description": "e.g. 'wants king size blue bedsheet under 2000 for gifting'"},
            },
            "required": ["stage"],
        },
    },
    {
        "name": "handoff_to_human",
        "description": "Pause the bot and assign this chat to the human team. Use for complaints, refunds, damaged items, explicit requests for a person, or anything you cannot resolve.",
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string", "description": "One line for the team"}},
            "required": ["reason"],
        },
    },
]

SEARCH_KNOWLEDGE_TOOL = {
    "name": "search_knowledge",
    "description": "Search the brand's policies and FAQs (shipping, returns, COD, care, sizing).",
    "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
}


def tool_definitions(knowledge_inline: bool) -> list[dict]:
    return TOOL_DEFINITIONS if knowledge_inline else [*TOOL_DEFINITIONS, SEARCH_KNOWLEDGE_TOOL]


class ToolFailure(Exception):
    """An expected failure whose message is shown to the model as an error tool_result."""


# --- implementations ---


async def _search_products(ctx: ToolContext, inp: SearchProductsInput) -> Any:
    products = await catalog.search_products(
        ctx.session,
        ctx.tenant.id,
        inp.query,
        Decimal(str(inp.max_price)) if inp.max_price else None,
        inp.limit,
    )
    if not products:
        return {"results": [], "note": "No matching products. Ask a clarifying question or suggest a related category."}
    return {"results": [catalog.product_summary(p) for p in products]}


async def _get_product_details(ctx: ToolContext, inp: ProductIdInput) -> Any:
    product = await catalog.get_product(ctx.session, ctx.tenant.id, inp.product_id)
    if not product or not product.active:
        raise ToolFailure(f"No active product with id {inp.product_id}")
    return catalog.product_summary(product, with_description=True)


async def _send_product_images(ctx: ToolContext, inp: SendImagesInput) -> Any:
    sent, missing = [], []
    for pid in inp.product_ids:
        product = await catalog.get_product(ctx.session, ctx.tenant.id, pid)
        if not product or not product.active or not product.image_url:
            missing.append(pid)
            continue
        summary = catalog.product_summary(product)
        caption = f"*{product.title}*\n{summary['price']}"
        if summary.get("mrp"):
            caption += f" (MRP {summary['mrp']}, {summary['discount_percent']}% off)"
        if product.url:
            caption += f"\n{product.url}"
        try:
            await send_message(ctx.session, ctx.sender, ctx.conv, ctx.contact, image_message(product.image_url, caption), by="bot")
        except WhatsAppError as exc:
            # Don't fail (and later retry) the whole turn over a photo.
            log.warning("image send failed for %s: %s", pid, exc)
            missing.append(pid)
            continue
        sent.append(pid)
        ctx.events.append({"type": "image_sent", "product_id": pid})
    return {"sent": sent, "not_sent_no_image": missing, "note": "The images are already delivered; don't repeat their captions."}


async def _resolve_items(ctx: ToolContext, items: list[Item]) -> tuple[list[OrderItemInput], list[dict], Decimal]:
    resolved, lines, total = [], [], Decimal("0")
    for it in items:
        product = await catalog.get_product(ctx.session, ctx.tenant.id, it.product_id)
        if not product or not product.active:
            raise ToolFailure(f"Product {it.product_id} not found")
        price = product.price
        title = product.title
        if product.variants:
            variant = next((v for v in product.variants if v["id"] == it.variant_id), None)
            if variant is None:
                options = ", ".join(f"{v['id']} ({v['title']})" for v in product.variants[:15])
                raise ToolFailure(f"'{product.title}' needs a variant_id. Options: {options}")
            if not variant.get("in_stock", True):
                raise ToolFailure(f"Variant {variant['title']} of '{product.title}' is out of stock")
            price = Decimal(str(variant["price"]))
            title = f"{product.title} - {variant['title']}"
        elif not product.in_stock:
            raise ToolFailure(f"'{product.title}' is out of stock")
        resolved.append(OrderItemInput(product_id=product.external_id, variant_id=it.variant_id, quantity=it.quantity))
        lines.append({"title": title, "quantity": it.quantity, "price": str(price)})
        total += price * it.quantity
    return resolved, lines, total


def _require_store(ctx: ToolContext) -> StoreConnector:
    if ctx.connector is None:
        raise ToolFailure("The store is not connected yet. Share the product URL instead.")
    return ctx.connector


async def _create_checkout_link(ctx: ToolContext, inp: CheckoutInput) -> Any:
    items, lines, total = await _resolve_items(ctx, inp.items)
    connector = _require_store(ctx)
    try:
        url = await connector.create_checkout_link(items)
    except StoreError as exc:
        raise ToolFailure(str(exc)) from exc
    await _set_stage(ctx, "checkout_sent", None)
    ctx.events.append({"type": "checkout_link", "url": url})
    return {"url": url, "items": lines, "estimated_total": f"₹{total:,.0f}", "note": "Shipping/discounts are applied at checkout."}


_PINCODE_RE = re.compile(r"^[1-9][0-9]{5}$")


async def _place_cod_order(ctx: ToolContext, inp: PlaceCodInput) -> Any:
    if not ctx.cfg.allow_cod_orders_in_chat:
        raise ToolFailure("COD orders in chat are disabled for this brand. Offer a checkout link instead.")
    if not inp.customer_confirmed:
        raise ToolFailure("Send the order summary and wait for the customer to confirm before placing the order.")
    pincode = re.sub(r"\s", "", inp.customer.pincode)
    if not _PINCODE_RE.match(pincode):
        raise ToolFailure("Pincode must be 6 digits. Ask the customer to check it.")
    items, lines, total = await _resolve_items(ctx, inp.items)
    phone = normalize_phone(inp.customer.phone) if inp.customer.phone else ctx.contact.wa_id
    customer = CustomerInput(
        name=inp.customer.name.strip(),
        phone=phone,
        address_line1=inp.customer.address_line1.strip(),
        address_line2=inp.customer.address_line2.strip(),
        city=inp.customer.city.strip(),
        state=inp.customer.state.strip(),
        pincode=pincode,
        email=inp.customer.email,
    )
    if ctx.dry_run:
        ctx.events.append({"type": "order_simulated", "items": lines, "total": str(total)})
        return {
            "order_number": "TEST-0001",
            "status": "simulated (playground: no real order was created)",
            "items": lines,
            "total": f"₹{total:,.0f}",
            "payment": "Cash on Delivery",
        }
    connector = _require_store(ctx)
    # Never place the same order twice (e.g. the model repeats the call, or a retried job re-runs the turn).
    recent = await ctx.session.scalar(
        select(Order)
        .where(
            Order.tenant_id == ctx.tenant.id,
            Order.contact_id == ctx.contact.id,
            Order.created_via == "bot",
            Order.total == total,
            Order.created_at > func.now() - timedelta(minutes=30),
        )
        .order_by(Order.id.desc())
        .limit(1)
    )
    if recent is not None and [(i["title"], i["quantity"]) for i in recent.items] == [(i["title"], i["quantity"]) for i in lines]:
        return {"order_number": recent.number, "status": recent.status, "total": f"₹{recent.total:,.0f}", "payment": "Cash on Delivery",
                "note": "This order was already placed a moment ago; it was not placed again."}
    try:
        order = await connector.create_cod_order(customer, items, note=f"Placed via WhatsApp assistant for +{ctx.contact.wa_id}")
    except StoreError as exc:
        log.warning("COD order failed for tenant %s: %s", ctx.tenant.id, exc)
        raise ToolFailure(f"The store rejected the order: {exc}. Apologise and hand over to a human.") from exc
    order.items = lines  # keep our line titles so the duplicate check above matches
    row, _ = await upsert_order(ctx.session, ctx.tenant.id, order, contact_id=ctx.contact.id, created_via="bot")
    # The customer already confirmed in chat; no separate confirmation message needed.
    row.cod_status = "confirmed"
    await _set_stage(ctx, "ordered", None)
    # Persist now: the order exists in the store even if a later step of this turn fails.
    await ctx.session.commit()
    ctx.events.append({"type": "order_placed", "number": order.number})
    return {"order_number": order.number, "status": order.status, "total": f"₹{order.total:,.0f}", "payment": "Cash on Delivery"}


async def _track_order(ctx: ToolContext, inp: TrackOrderInput) -> Any:
    orders, note = await lookup_customer_orders(ctx.session, ctx.tenant.id, ctx.connector, ctx.contact, inp.order_number)
    result: dict = {"orders": [order_view(o) for o in orders]}
    if note:
        result["note"] = note
    return result


async def _set_stage(ctx: ToolContext, stage: str, note: str | None) -> None:
    set_lead_stage(ctx.contact, stage, note)


async def _update_lead(ctx: ToolContext, inp: UpdateLeadInput) -> Any:
    await _set_stage(ctx, inp.stage, inp.note or None)
    return {"ok": True, "stage": ctx.contact.lead_stage}


async def _handoff(ctx: ToolContext, inp: HandoffInput) -> Any:
    ctx.conv.mode = "human"
    ctx.conv.handoff_reason = inp.reason
    ctx.events.append({"type": "handoff", "reason": inp.reason})
    return {"ok": True, "note": f"The team has been notified. Support hours: {ctx.cfg.support_hours}. Tell the customer briefly; do not promise an exact time outside those hours."}


async def _search_knowledge(ctx: ToolContext, inp: SearchKnowledgeInput) -> Any:
    terms = catalog.query_terms(inp.query) or [inp.query.lower()]
    stmt = (
        select(KnowledgeDoc)
        .where(KnowledgeDoc.tenant_id == ctx.tenant.id)
        .where(or_(*(KnowledgeDoc.content.ilike(f"%{t}%") for t in terms), *(KnowledgeDoc.title.ilike(f"%{t}%") for t in terms)))
        .limit(4)
    )
    docs = (await ctx.session.scalars(stmt)).all()
    return {"results": [{"title": d.title, "content": d.content[:3000]} for d in docs]}


_TOOLS: dict[str, tuple[type[BaseModel], Any]] = {
    "search_products": (SearchProductsInput, _search_products),
    "get_product_details": (ProductIdInput, _get_product_details),
    "send_product_images": (SendImagesInput, _send_product_images),
    "create_checkout_link": (CheckoutInput, _create_checkout_link),
    "place_cod_order": (PlaceCodInput, _place_cod_order),
    "track_order": (TrackOrderInput, _track_order),
    "update_lead": (UpdateLeadInput, _update_lead),
    "handoff_to_human": (HandoffInput, _handoff),
    "search_knowledge": (SearchKnowledgeInput, _search_knowledge),
}


async def execute_tool(ctx: ToolContext, name: str, raw_input: Any) -> tuple[str, bool]:
    """Returns (result_text, is_error)."""
    spec = _TOOLS.get(name)
    if spec is None:
        return f"Unknown tool {name}", True
    model, fn = spec
    try:
        inp = model.model_validate(raw_input if isinstance(raw_input, dict) else {})
    except ValidationError as exc:
        return f"Invalid input for {name}: {exc.errors(include_url=False)}", True
    try:
        result = await fn(ctx, inp)
    except ToolFailure as exc:
        return str(exc), True
    return json.dumps(result, ensure_ascii=False, default=str), False
