"""Custom store connector: any store (Next.js, Laravel, Django...) that implements the
WA Agent Store API v1. The drop-in Next.js implementation lives in integrations/nextjs.

Every request in both directions is signed:
    X-WA-Agent-Timestamp: unix seconds
    X-WA-Agent-Signature: sha256=hex(HMAC_SHA256(secret, f"{ts}.{METHOD}.{path_with_query}.{body}"))
"""

import json
from collections.abc import AsyncIterator
from urllib.parse import urlencode, urlsplit

import httpx

from app.connectors.base import (
    CustomerInput,
    OrderData,
    OrderItemInput,
    ProductData,
    StoreConnector,
    StoreError,
    StoreEvent,
    VariantData,
    money,
    normalize_phone,
    optional_money,
)
from app.security import sign_request, verify_request_signature


def product_from_json(p: dict) -> ProductData:
    variants = [
        VariantData(
            id=str(v["id"]),
            title=v.get("title") or "",
            price=money(v.get("price", p.get("price"))),
            in_stock=bool(v.get("in_stock", True)),
            sku=v.get("sku"),
        )
        for v in p.get("variants") or []
    ]
    return ProductData(
        external_id=str(p["id"]),
        title=p["title"],
        description=p.get("description") or "",
        price=money(p.get("price")),
        compare_at_price=optional_money(p.get("compare_at_price")),
        currency=p.get("currency") or "INR",
        url=p.get("url"),
        image_url=p.get("image_url"),
        in_stock=bool(p.get("in_stock", True)),
        stock=p.get("stock"),
        category=p.get("category"),
        tags=[str(t) for t in p.get("tags") or []],
        variants=variants,
        active=bool(p.get("active", True)),
    )


def order_from_json(o: dict) -> OrderData:
    customer = o.get("customer") or {}
    return OrderData(
        external_id=str(o["id"]),
        number=str(o.get("number") or o["id"]),
        status=o.get("status") or "unknown",
        payment_method="cod" if (o.get("payment_method") or "").lower() == "cod" else "prepaid",
        total=money(o.get("total")),
        currency=o.get("currency") or "INR",
        customer_name=customer.get("name"),
        customer_phone=normalize_phone(customer.get("phone")),
        items=[
            {"title": i.get("title"), "quantity": i.get("quantity", 1), "price": str(money(i.get("price")))}
            for i in o.get("items") or []
        ],
        tracking=o.get("tracking") or {},
        created_at=o.get("created_at"),
    )


class CustomStoreConnector(StoreConnector):
    kind = "custom"

    def __init__(self, base_url: str, secret: str, http: httpx.AsyncClient):
        self.base_url = base_url.rstrip("/")
        self.base_path = urlsplit(self.base_url).path
        self.secret = secret
        self.http = http

    async def _request(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict | None:
        query = f"?{urlencode(params)}" if params else ""
        raw = json.dumps(body).encode() if body is not None else b""
        headers = sign_request(self.secret, method, f"{self.base_path}{path}{query}", raw)
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            resp = await self.http.request(method, f"{self.base_url}{path}{query}", content=raw or None, headers=headers, timeout=30)
        except httpx.HTTPError as exc:
            raise StoreError(f"Store unreachable: {exc}") from exc
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("error") or resp.text[:300]
            except ValueError:
                detail = resp.text[:300]
            raise StoreError(f"Store API {resp.status_code}: {detail}", permanent=400 <= resp.status_code < 500)
        return resp.json()

    async def iter_products(self) -> AsyncIterator[ProductData]:
        cursor: str | None = None
        while True:
            params = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            data = await self._request("GET", "/products", params=params) or {}
            for p in data.get("products", []):
                yield product_from_json(p)
            cursor = data.get("next_cursor")
            if not cursor:
                break

    async def get_order(self, ref: str) -> OrderData | None:
        data = await self._request("GET", f"/orders/{ref.lstrip('#')}")
        return order_from_json(data["order"]) if data and data.get("order") else None

    async def find_orders_by_phone(self, phone: str) -> list[OrderData]:
        data = await self._request("GET", "/orders", params={"phone": normalize_phone(phone)}) or {}
        return [order_from_json(o) for o in data.get("orders", [])]

    async def create_cod_order(self, customer: CustomerInput, items: list[OrderItemInput], note: str = "") -> OrderData:
        body = {
            "payment_method": "cod",
            "source": "whatsapp",
            "note": note,
            "customer": {
                "name": customer.name,
                "phone": normalize_phone(customer.phone),
                "email": customer.email,
                "address_line1": customer.address_line1,
                "address_line2": customer.address_line2,
                "city": customer.city,
                "state": customer.state,
                "pincode": customer.pincode,
            },
            "items": [{"product_id": i.product_id, "variant_id": i.variant_id, "quantity": i.quantity} for i in items],
        }
        data = await self._request("POST", "/orders", body=body)
        if not data or not data.get("order"):
            raise StoreError("Store did not return the created order")
        return order_from_json(data["order"])

    async def create_checkout_link(self, items: list[OrderItemInput]) -> str:
        body = {"items": [{"product_id": i.product_id, "variant_id": i.variant_id, "quantity": i.quantity} for i in items]}
        data = await self._request("POST", "/checkout-links", body=body)
        if not data or not data.get("url"):
            raise StoreError("Store did not return a checkout url")
        return data["url"]

    async def confirm_cod_order(self, external_id: str) -> None:
        await self._request("POST", f"/orders/{external_id}/confirm-cod", body={})

    async def cancel_order(self, external_id: str, reason: str) -> None:
        await self._request("POST", f"/orders/{external_id}/cancel", body={"reason": reason})

    def verify_webhook(self, headers: dict[str, str], body: bytes, path_with_query: str) -> bool:
        return verify_request_signature(
            self.secret,
            "POST",
            path_with_query,
            body,
            headers.get("x-wa-agent-timestamp"),
            headers.get("x-wa-agent-signature"),
        )

    def parse_webhook(self, headers: dict[str, str], body: bytes) -> list[StoreEvent]:
        data = json.loads(body)
        event = StoreEvent(event_id=str(data.get("event_id") or ""), type=data.get("type", ""))
        if data.get("order"):
            event.order = order_from_json(data["order"])
        if data.get("product"):
            event.product = product_from_json(data["product"])
        event.product_id = data.get("product_id") or (event.product.external_id if event.product else None)
        if not event.event_id:
            # Fall back to a content-derived key so retries are still deduplicated.
            ref = event.order.external_id if event.order else event.product_id
            event.event_id = f"{event.type}:{ref}:{data.get('updated_at', '')}"
        return [event]
