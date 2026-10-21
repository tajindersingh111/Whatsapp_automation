"""Product catalog mirror: synced from the store, searched by the AI agent."""

import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import case, func, literal, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import ProductData, StoreConnector
from app.models import Product

# Latin + Devanagari letters and digits (no underscore: it is a LIKE wildcard).
_WORD_RE = re.compile("[0-9A-Za-zÀ-ɏऀ-ॿ]+")
# Words that carry no product meaning in Hinglish/English shopping chats.
_STOPWORDS = {
    "a", "an", "the", "for", "and", "or", "of", "to", "in", "on", "with", "me", "my", "i", "want", "need",
    "show", "some", "any", "please", "pls", "plz", "chahiye", "dikhao", "dikha", "do", "ka", "ki", "ke",
    "hai", "h", "koi", "kuch", "mujhe", "liye", "wala", "wali", "vala", "vali", "under", "below", "rs",
    "kya", "kaise", "aap", "ap", "hain", "haan", "ha", "nahi", "ok", "okay", "thik", "acha", "accha", "bhai",
    "ji", "sir", "mam", "maam", "bhejo", "batao", "bataiye", "price", "rate", "kitna", "kitne", "kitni", "available",
    "is", "it", "this", "that", "have", "you", "do", "can", "get", "hi", "hello",
}


def _search_text(p: ProductData) -> str:
    parts = [p.title, p.category or "", " ".join(p.tags), " ".join(v.title for v in p.variants), p.description[:600]]
    return " ".join(parts).lower()


def _values(tenant_id: uuid.UUID, p: ProductData) -> dict:
    return {
        "tenant_id": tenant_id,
        "external_id": p.external_id,
        "title": p.title[:500],
        "description": p.description,
        "price": p.price,
        "compare_at_price": p.compare_at_price,
        "currency": p.currency,
        "url": p.url,
        "image_url": p.image_url,
        "in_stock": p.in_stock,
        "stock": p.stock,
        "category": p.category,
        "tags": p.tags,
        "variants": [v.to_json() for v in p.variants],
        "active": p.active,
        "search_text": _search_text(p),
        "synced_at": datetime.now(timezone.utc),
    }


async def upsert_product(session: AsyncSession, tenant_id: uuid.UUID, p: ProductData) -> None:
    values = _values(tenant_id, p)
    stmt = insert(Product).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["tenant_id", "external_id"],
        set_={k: stmt.excluded[k] for k in values if k not in ("tenant_id", "external_id")},
    )
    await session.execute(stmt)


async def deactivate_product(session: AsyncSession, tenant_id: uuid.UUID, external_id: str) -> None:
    await session.execute(
        update(Product).where(Product.tenant_id == tenant_id, Product.external_id == external_id).values(active=False)
    )


async def sync_catalog(session: AsyncSession, tenant_id: uuid.UUID, connector: StoreConnector) -> int:
    started = datetime.now(timezone.utc)
    count = 0
    async for product in connector.iter_products():
        await upsert_product(session, tenant_id, product)
        count += 1
        if count % 200 == 0:
            await session.flush()
    # Anything the store no longer returns is hidden from the bot.
    await session.execute(
        update(Product).where(Product.tenant_id == tenant_id, Product.synced_at < started).values(active=False)
    )
    return count


def query_terms(query: str) -> list[str]:
    words = [w.lower() for w in _WORD_RE.findall(query or "")]
    terms = [w for w in words if len(w) >= 2 and w not in _STOPWORDS and not w.isdigit()]
    return list(dict.fromkeys(terms))[:8]


async def search_products(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    query: str,
    max_price: Decimal | None = None,
    limit: int = 5,
    in_stock_only: bool = False,
) -> list[Product]:
    terms = query_terms(query)
    stmt = select(Product).where(Product.tenant_id == tenant_id, Product.active.is_(True))
    if max_price is not None:
        stmt = stmt.where(Product.price <= max_price)
    if in_stock_only:
        stmt = stmt.where(Product.in_stock.is_(True))
    if terms:
        cleaned = " ".join(terms)
        hits = sum((case((Product.search_text.ilike(f"%{t}%"), 1), else_=0) for t in terms), literal(0))
        title_hits = sum((case((Product.title.ilike(f"%{t}%"), 1), else_=0) for t in terms), literal(0))
        similarity = func.word_similarity(cleaned, Product.search_text)
        score = hits * 2 + title_hits + similarity * 3
        stmt = stmt.where(or_(*(Product.search_text.ilike(f"%{t}%") for t in terms), similarity > 0.35))
        stmt = stmt.order_by(score.desc(), Product.in_stock.desc(), Product.price)
    else:
        stmt = stmt.order_by(Product.in_stock.desc(), Product.synced_at.desc())
    return list((await session.scalars(stmt.limit(max(1, min(limit, 10))))).all())


async def get_product(session: AsyncSession, tenant_id: uuid.UUID, external_id: str) -> Product | None:
    return await session.scalar(
        select(Product).where(Product.tenant_id == tenant_id, Product.external_id == str(external_id))
    )


def product_summary(p: Product, with_description: bool = False) -> dict:
    data = {
        "product_id": p.external_id,
        "title": p.title,
        "price": f"₹{p.price:,.0f}" if p.currency == "INR" else f"{p.price} {p.currency}",
        "in_stock": p.in_stock,
        "url": p.url,
        "has_image": bool(p.image_url),
    }
    if p.compare_at_price and p.compare_at_price > p.price:
        data["mrp"] = f"₹{p.compare_at_price:,.0f}"
        data["discount_percent"] = int(round((1 - p.price / p.compare_at_price) * 100))
    if p.category:
        data["category"] = p.category
    if p.variants:
        data["variants"] = [
            {"variant_id": v["id"], "title": v["title"], "price": v["price"], "in_stock": v["in_stock"]} for v in p.variants[:15]
        ]
    if with_description:
        data["description"] = p.description[:1500]
        data["tags"] = p.tags
        if p.stock is not None:
            data["stock"] = p.stock
    return data
