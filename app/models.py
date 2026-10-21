"""Database schema. Every business table carries tenant_id so one deployment serves many brands."""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Tenant(TimestampMixin, Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Bot brain + flow settings; shape is app.bot_config.BotConfig.
    config: Mapped[dict] = mapped_column(JSONB, default=dict)


class WhatsAppAccount(TimestampMixin, Base):
    __tablename__ = "whatsapp_accounts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), unique=True)
    phone_number_id: Mapped[str] = mapped_column(String(64), unique=True)
    waba_id: Mapped[str | None] = mapped_column(String(64))
    display_phone: Mapped[str | None] = mapped_column(String(32))
    access_token_enc: Mapped[str] = mapped_column(Text)


class StoreConnection(TimestampMixin, Base):
    __tablename__ = "store_connections"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), unique=True)
    kind: Mapped[str] = mapped_column(String(20))  # custom | shopify | woocommerce
    base_url: Mapped[str] = mapped_column(String(500))
    # Encrypted JSON: api keys / tokens / shared secret, connector specific.
    credentials_enc: Mapped[str] = mapped_column(Text)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("tenant_id", "external_id"),
        Index("ix_products_search_trgm", "search_text", postgresql_using="gin", postgresql_ops={"search_text": "gin_trgm_ops"}),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    external_id: Mapped[str] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str] = mapped_column(Text, default="")
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    compare_at_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    url: Mapped[str | None] = mapped_column(String(1000))
    image_url: Mapped[str | None] = mapped_column(String(1000))
    in_stock: Mapped[bool] = mapped_column(Boolean, default=True)
    stock: Mapped[int | None] = mapped_column(Integer)
    category: Mapped[str | None] = mapped_column(String(200))
    tags: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    # [{id, title, price, in_stock, sku}]
    variants: Mapped[list] = mapped_column(JSONB, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    search_text: Mapped[str] = mapped_column(Text, default="")
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class KnowledgeDoc(TimestampMixin, Base):
    __tablename__ = "knowledge_docs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    content: Mapped[str] = mapped_column(Text)


class Correction(TimestampMixin, Base):
    """A reviewed answer: 'when customers ask X, answer Y'. This is how a brand trains its bot."""

    __tablename__ = "corrections"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    source_message_id: Mapped[int | None] = mapped_column(BigInteger)


LEAD_STAGES = ("new", "browsing", "interested", "checkout_sent", "ordered", "lost")


class Contact(TimestampMixin, Base):
    __tablename__ = "contacts"
    __table_args__ = (UniqueConstraint("tenant_id", "wa_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    wa_id: Mapped[str] = mapped_column(String(32))  # E.164 digits without '+', e.g. 919876543210
    name: Mapped[str | None] = mapped_column(String(200))
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    opted_out: Mapped[bool] = mapped_column(Boolean, default=False)
    lead_stage: Mapped[str] = mapped_column(String(20), default="new")
    lead_note: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(String(100))  # ctwa ad id, website, store order...


class Conversation(TimestampMixin, Base):
    __tablename__ = "conversations"
    __table_args__ = (UniqueConstraint("tenant_id", "contact_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"))
    mode: Mapped[str] = mapped_column(String(10), default="bot")  # bot | human
    handoff_reason: Mapped[str | None] = mapped_column(Text)
    is_playground: Mapped[bool] = mapped_column(Boolean, default=False)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    followups_sent: Mapped[int] = mapped_column(Integer, default=0)
    # Menu bot memory: {"awaiting": "order_number", "last_product": "p1", "misses": 1}
    state: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_conversation", "conversation_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    direction: Mapped[str] = mapped_column(String(3))  # in | out
    sender: Mapped[str] = mapped_column(String(10))  # customer | bot | human | flow
    kind: Mapped[str] = mapped_column(String(20), default="text")  # text | image | interactive | template | button | ...
    body: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    wa_message_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="received")  # received | queued | sent | delivered | read | failed | simulated
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Order(TimestampMixin, Base):
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("tenant_id", "external_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"))
    external_id: Mapped[str] = mapped_column(String(100))
    number: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(40))
    payment_method: Mapped[str] = mapped_column(String(20))  # cod | prepaid
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    customer_name: Mapped[str | None] = mapped_column(String(200))
    customer_phone: Mapped[str | None] = mapped_column(String(32))
    items: Mapped[list] = mapped_column(JSONB, default=list)
    tracking: Mapped[dict] = mapped_column(JSONB, default=dict)
    # COD confirmation flow state: pending | confirmed | cancelled | no_response
    cod_status: Mapped[str | None] = mapped_column(String(20))
    cod_reminders_sent: Mapped[int] = mapped_column(Integer, default=0)
    shipping_notified_status: Mapped[str | None] = mapped_column(String(40))
    created_via: Mapped[str] = mapped_column(String(10), default="store")  # store | bot


class Job(Base):
    """Postgres-backed job queue (SELECT ... FOR UPDATE SKIP LOCKED). No Redis needed."""

    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_ready", "status", "run_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(60))
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(10), default="queued")  # queued | running | done | failed
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    last_error: Mapped[str | None] = mapped_column(Text)
    dedupe_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WebhookEvent(Base):
    """Idempotency ledger: Shopify, WooCommerce and Meta all retry webhooks."""

    __tablename__ = "webhook_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    source: Mapped[str] = mapped_column(String(20))
    event_key: Mapped[str] = mapped_column(String(300), unique=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AIUsage(Base):
    """Token usage per tenant, so each client's AI cost can be billed with margin."""

    __tablename__ = "ai_usage"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[int | None] = mapped_column(BigInteger)
    model: Mapped[str] = mapped_column(String(60))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
