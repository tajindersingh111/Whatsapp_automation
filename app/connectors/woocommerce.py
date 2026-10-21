"""WooCommerce connector (REST API v3).

Setup per client: WooCommerce > Settings > Advanced > REST API, create a Read/Write key.
Webhooks (Settings > Advanced > Webhooks): order.created, order.updated, product.created,
product.updated, product.deleted -> {PUBLIC_BASE_URL}/webhooks/store/{tenant_id}, all with
the same secret. Tracking is read from the Advanced Shipment Tracking plugin meta if present.
"""

import json
from collections.abc import AsyncIterator

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
    split_name,
    strip_html,
)
from app.security import verify_base64_hmac


def _product(p: dict, variations: list[dict] | None = None) -> ProductData:
    images = p.get("images") or []
    variants = [
        VariantData(
            id=str(v["id"]),
            title=" / ".join(a.get("option", "") for a in v.get("attributes", [])) or str(v["id"]),
            price=money(v.get("price")),
            in_stock=v.get("stock_status") == "instock",
            sku=v.get("sku") or None,
        )
        for v in variations or []
    ]
    return ProductData(
        external_id=str(p["id"]),
        title=p.get("name", ""),
        description=strip_html(p.get("short_description") or "") + " " + strip_html(p.get("description") or "", 1500),
        price=money(p.get("price")),
        compare_at_price=optional_money(p.get("regular_price")) if p.get("sale_price") else None,
        url=p.get("permalink"),
        image_url=images[0]["src"] if images else None,
        in_stock=p.get("stock_status") == "instock",
        stock=p.get("stock_quantity"),
        category=(p.get("categories") or [{}])[0].get("name"),
        tags=[t["name"] for t in p.get("tags") or []],
        variants=variants,
        active=p.get("status", "publish") == "publish",
    )


def _tracking(meta: list[dict]) -> dict:
    for m in meta or []:
        if m.get("key") == "_wc_shipment_tracking_items" and m.get("value"):
            item = m["value"][-1]
            return {
                "courier": item.get("tracking_provider") or item.get("custom_tracking_provider"),
                "awb": item.get("tracking_number"),
                "url": item.get("custom_tracking_link") or item.get("tracking_link"),
                "status": "shipped",
            }
    return {}


def _order(o: dict) -> OrderData:
    billing = o.get("billing") or {}
    shipping = o.get("shipping") or {}
    name = " ".join(filter(None, [shipping.get("first_name") or billing.get("first_name"), shipping.get("last_name") or billing.get("last_name")]))
    return OrderData(
        external_id=str(o["id"]),
        number=str(o.get("number") or o["id"]),
        status=o.get("status", "unknown"),
        payment_method="cod" if o.get("payment_method") == "cod" else "prepaid",
        total=money(o.get("total")),
        currency=o.get("currency") or "INR",
        customer_name=name or None,
        customer_phone=normalize_phone(shipping.get("phone") or billing.get("phone")),
        items=[{"title": li.get("name"), "quantity": li.get("quantity"), "price": str(money(li.get("price")))} for li in o.get("line_items") or []],
        tracking=_tracking(o.get("meta_data") or []),
        created_at=o.get("date_created_gmt"),
    )


class WooCommerceConnector(StoreConnector):
    kind = "woocommerce"

    def __init__(self, site_url: str, consumer_key: str, consumer_secret: str, webhook_secret: str, http: httpx.AsyncClient):
        self.site_url = site_url.rstrip("/")
        self.api = f"{self.site_url}/wp-json/wc/v3"
        self.auth = (consumer_key, consumer_secret)
        self.webhook_secret = webhook_secret
        self.http = http

    async def _request(self, method: str, path: str, params: dict | None = None, body: dict | None = None):
        try:
            resp = await self.http.request(method, f"{self.api}{path}", params=params, json=body, auth=self.auth, timeout=30)
        except httpx.HTTPError as exc:
            raise StoreError(f"WooCommerce unreachable: {exc}") from exc
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("message")
            except ValueError:
                detail = resp.text[:300]
            raise StoreError(f"WooCommerce {resp.status_code}: {detail}", permanent=400 <= resp.status_code < 500)
        return resp.json()

    async def iter_products(self) -> AsyncIterator[ProductData]:
        page = 1
        while True:
            batch = await self._request("GET", "/products", params={"per_page": 100, "page": page, "status": "publish"}) or []
            for p in batch:
                variations = None
                if p.get("type") == "variable":
                    variations = await self._request("GET", f"/products/{p['id']}/variations", params={"per_page": 100}) or []
                yield _product(p, variations)
            if len(batch) < 100:
                break
            page += 1

    async def get_order(self, ref: str) -> OrderData | None:
        ref = ref.strip().lstrip("#")
        if ref.isdigit():
            data = await self._request("GET", f"/orders/{ref}")
            if data:
                return _order(data)
        found = await self._request("GET", "/orders", params={"search": ref, "per_page": 1}) or []
        return _order(found[0]) if found else None

    async def find_orders_by_phone(self, phone: str) -> list[OrderData]:
        found = await self._request("GET", "/orders", params={"search": normalize_phone(phone)[-10:], "per_page": 5}) or []
        return [_order(o) for o in found]

    async def create_cod_order(self, customer: CustomerInput, items: list[OrderItemInput], note: str = "") -> OrderData:
        first, last = split_name(customer.name)
        address = {
            "first_name": first,
            "last_name": last,
            "address_1": customer.address_line1,
            "address_2": customer.address_line2,
            "city": customer.city,
            "state": customer.state,
            "postcode": customer.pincode,
            "country": "IN",
            "phone": "+" + normalize_phone(customer.phone),
        }
        body = {
            "payment_method": "cod",
            "payment_method_title": "Cash on Delivery",
            "set_paid": False,
            "status": "processing",
            "billing": {**address, "email": customer.email or ""},
            "shipping": address,
            "customer_note": note,
            "line_items": [
                {"product_id": int(i.product_id), "quantity": i.quantity, **({"variation_id": int(i.variant_id)} if i.variant_id else {})}
                for i in items
            ],
            "meta_data": [{"key": "_created_via_whatsapp_agent", "value": "yes"}],
        }
        data = await self._request("POST", "/orders", body=body)
        if not data:
            raise StoreError("WooCommerce did not return the created order")
        return _order(data)

    async def create_checkout_link(self, items: list[OrderItemInput]) -> str:
        if len(items) != 1:
            raise StoreError(
                "WooCommerce checkout links support one product at a time; send product links or place a COD order instead",
                permanent=True,
            )
        item = items[0]
        return f"{self.site_url}/checkout/?add-to-cart={item.variant_id or item.product_id}&quantity={item.quantity}"

    async def confirm_cod_order(self, external_id: str) -> None:
        await self._request("POST", f"/orders/{external_id}/notes", body={"note": "COD order confirmed by customer on WhatsApp."})
        await self._request("PUT", f"/orders/{external_id}", body={"meta_data": [{"key": "_cod_confirmed", "value": "yes"}]})

    async def cancel_order(self, external_id: str, reason: str) -> None:
        await self._request("PUT", f"/orders/{external_id}", body={"status": "cancelled"})
        await self._request("POST", f"/orders/{external_id}/notes", body={"note": f"Cancelled via WhatsApp: {reason}"})

    def verify_webhook(self, headers: dict[str, str], body: bytes, path_with_query: str) -> bool:
        return verify_base64_hmac(body, headers.get("x-wc-webhook-signature"), self.webhook_secret)

    def parse_webhook(self, headers: dict[str, str], body: bytes) -> list[StoreEvent]:
        topic = headers.get("x-wc-webhook-topic", "")
        delivery = headers.get("x-wc-webhook-delivery-id", "")
        if not body.strip().startswith(b"{"):
            return []  # WooCommerce sends a form-encoded ping when a webhook is created
        data = json.loads(body)
        event_id = f"{topic}:{delivery}" if delivery else f"{topic}:{data.get('id')}:{data.get('date_modified_gmt')}"
        if topic in ("order.created", "order.updated"):
            return [StoreEvent(event_id=event_id, type=topic, order=_order(data))]
        if topic in ("product.created", "product.updated"):
            return [StoreEvent(event_id=event_id, type="product.updated", product=_product(data), product_id=str(data["id"]))]
        if topic == "product.deleted":
            return [StoreEvent(event_id=event_id, type="product.deleted", product_id=str(data.get("id")))]
        return []
