"""Common store interface. Each e-commerce platform maps its API onto these shapes."""

import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation


class StoreError(Exception):
    def __init__(self, message: str, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


@dataclass
class VariantData:
    id: str
    title: str
    price: Decimal
    in_stock: bool = True
    sku: str | None = None

    def to_json(self) -> dict:
        return {"id": self.id, "title": self.title, "price": str(self.price), "in_stock": self.in_stock, "sku": self.sku}


@dataclass
class ProductData:
    external_id: str
    title: str
    price: Decimal
    description: str = ""
    compare_at_price: Decimal | None = None
    currency: str = "INR"
    url: str | None = None
    image_url: str | None = None
    in_stock: bool = True
    stock: int | None = None
    category: str | None = None
    tags: list[str] = field(default_factory=list)
    variants: list[VariantData] = field(default_factory=list)
    active: bool = True


@dataclass
class OrderItemInput:
    product_id: str
    quantity: int = 1
    variant_id: str | None = None


@dataclass
class CustomerInput:
    name: str
    phone: str
    address_line1: str
    city: str
    state: str
    pincode: str
    address_line2: str = ""
    email: str | None = None


@dataclass
class OrderData:
    external_id: str
    number: str
    status: str
    payment_method: str  # cod | prepaid
    total: Decimal
    currency: str = "INR"
    customer_name: str | None = None
    customer_phone: str | None = None
    items: list[dict] = field(default_factory=list)  # [{title, quantity, price}]
    # {courier, awb, url, status}; empty until shipped
    tracking: dict = field(default_factory=dict)
    created_at: str | None = None

    def to_json(self) -> dict:
        data = asdict(self)
        data["total"] = str(self.total)
        return data


@dataclass
class StoreEvent:
    event_id: str
    type: str  # order.created | order.updated | product.updated | product.deleted
    order: OrderData | None = None
    product: ProductData | None = None
    product_id: str | None = None


class StoreConnector(ABC):
    kind: str

    @abstractmethod
    def iter_products(self) -> AsyncIterator[ProductData]: ...

    @abstractmethod
    async def get_order(self, ref: str) -> OrderData | None:
        """Look up by order number (what customers see) or id."""

    @abstractmethod
    async def find_orders_by_phone(self, phone: str) -> list[OrderData]: ...

    @abstractmethod
    async def create_cod_order(self, customer: CustomerInput, items: list[OrderItemInput], note: str = "") -> OrderData: ...

    @abstractmethod
    async def create_checkout_link(self, items: list[OrderItemInput]) -> str: ...

    @abstractmethod
    async def confirm_cod_order(self, external_id: str) -> None: ...

    @abstractmethod
    async def cancel_order(self, external_id: str, reason: str) -> None: ...

    @abstractmethod
    def verify_webhook(self, headers: dict[str, str], body: bytes, path_with_query: str) -> bool: ...

    @abstractmethod
    def parse_webhook(self, headers: dict[str, str], body: bytes) -> list[StoreEvent]: ...


# --- helpers shared by connectors ---

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(html: str | None, limit: int = 2000) -> str:
    if not html:
        return ""
    text = _TAG_RE.sub(" ", html)
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&quot;", '"'), ("&#39;", "'"), ("&lt;", "<"), ("&gt;", ">")):
        text = text.replace(entity, char)
    return _WS_RE.sub(" ", text).strip()[:limit]


def money(value) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except InvalidOperation:
        return Decimal("0")


def optional_money(value) -> Decimal | None:
    if value is None or value == "":
        return None
    amount = money(value)
    return amount if amount > 0 else None


def normalize_phone(raw: str | None, default_country_code: str = "91") -> str:
    """'+91 98765-43210' / '09876543210' / '9876543210' -> '919876543210'."""
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return ""
    if digits.startswith("00"):  # 0091... is the international prefix written out
        digits = digits[2:]
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10:
        digits = default_country_code + digits
    return digits


def split_name(name: str) -> tuple[str, str]:
    parts = name.strip().split(None, 1)
    if not parts:
        return "", ""
    return parts[0], parts[1] if len(parts) > 1 else ""
