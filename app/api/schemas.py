from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class TenantCreate(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    slug: str | None = Field(default=None, pattern=r"^[a-z0-9-]{2,80}$")
    config: dict | None = None


class TenantUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=200)
    active: bool | None = None


class WhatsAppConnect(BaseModel):
    phone_number_id: str = Field(min_length=3)
    # Omit to keep the saved token.
    access_token: str | None = Field(default=None, min_length=10)
    waba_id: str | None = None
    display_phone: str | None = None


class TokenIn(BaseModel):
    access_token: str = Field(min_length=10)


class StoreConnect(BaseModel):
    kind: Literal["custom", "shopify", "woocommerce"]
    base_url: str = Field(min_length=8, description="custom: the store's /api/wa-agent URL; shopify: https://x.myshopify.com; woocommerce: site URL")
    # Omitted keys keep their saved values when the same platform is re-connected.
    credentials: dict = Field(default_factory=dict)
    # custom stores: issue a new shared secret (the store's WA_AGENT_SECRET must be updated too).
    rotate_secret: bool = False


class KnowledgeIn(BaseModel):
    title: str = Field(min_length=2, max_length=300)
    content: str = Field(min_length=2)


class CorrectionIn(BaseModel):
    question: str = Field(min_length=2)
    answer: str = Field(min_length=2)
    source_message_id: int | None = None


class HumanMessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=4096)


class ModeIn(BaseModel):
    mode: Literal["bot", "human"]


class PlaygroundButton(BaseModel):
    id: str = Field(min_length=1, max_length=256)
    title: str = Field(default="", max_length=100)


class PlaygroundIn(BaseModel):
    message: str = Field(default="", max_length=4096)
    # A tapped reply button / list row (menu bot).
    button: PlaygroundButton | None = None
    session: str = Field(default="default", pattern=r"^[A-Za-z0-9_-]{1,20}$")


class MessageOut(BaseModel):
    id: int
    direction: str
    sender: str
    kind: str
    body: str
    status: str
    error: str | None
    created_at: datetime
    payload: dict
