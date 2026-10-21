"""Shopify connector (GraphQL Admin API + REST-format webhooks).

Setup per client: create a custom app in the store admin with scopes
read_products, read_orders, write_orders; copy the Admin API access token and the
app's API secret (used to verify webhooks). Subscribe webhooks orders/create,
orders/updated, products/create, products/update, products/delete to
{PUBLIC_BASE_URL}/webhooks/store/{tenant_id}.

NOTE: written against the Shopify docs, not yet exercised against a live store.
Verify COD order creation on a development store before the first client.
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

API_VERSION = "2025-07"

_PRODUCTS_QUERY = """
query Products($cursor: String) {
  products(first: 100, after: $cursor, query: "status:active") {
    pageInfo { hasNextPage endCursor }
    nodes {
      legacyResourceId title handle onlineStoreUrl productType tags totalInventory
      description(truncateAt: 1500)
      featuredMedia { preview { image { url } } }
      variants(first: 50) {
        nodes { legacyResourceId title price compareAtPrice sku availableForSale }
      }
    }
  }
}
"""

_ORDER_FIELDS = """
  legacyResourceId name createdAt cancelledAt displayFinancialStatus displayFulfillmentStatus
  paymentGatewayNames phone
  totalPriceSet { shopMoney { amount currencyCode } }
  customer { displayName phone }
  shippingAddress { name phone }
  lineItems(first: 30) { nodes { title quantity originalUnitPriceSet { shopMoney { amount } } } }
  fulfillments(first: 5) { displayStatus trackingInfo(first: 1) { company number url } }
"""

_ORDERS_QUERY = "query Orders($q: String) { orders(first: 5, query: $q, sortKey: CREATED_AT, reverse: true) { nodes {" + _ORDER_FIELDS + "} } }"


def _is_cod(gateways: list[str]) -> bool:
    joined = " ".join(gateways).lower()
    return "cash on delivery" in joined or "cod" in joined.split()


def _gql_order(node: dict) -> OrderData:
    tracking: dict = {}
    for f in node.get("fulfillments") or []:
        info = (f.get("trackingInfo") or [{}])[0] if f.get("trackingInfo") else {}
        if info.get("number") or info.get("url"):
            tracking = {"courier": info.get("company"), "awb": info.get("number"), "url": info.get("url"), "status": f.get("displayStatus")}
    status = "cancelled" if node.get("cancelledAt") else (node.get("displayFulfillmentStatus") or "UNFULFILLED").lower()
    customer = node.get("customer") or {}
    ship = node.get("shippingAddress") or {}
    money_set = node.get("totalPriceSet", {}).get("shopMoney", {})
    return OrderData(
        external_id=str(node["legacyResourceId"]),
        number=node["name"],
        status=status,
        payment_method="cod" if _is_cod(node.get("paymentGatewayNames") or []) else "prepaid",
        total=money(money_set.get("amount")),
        currency=money_set.get("currencyCode") or "INR",
        customer_name=customer.get("displayName") or ship.get("name"),
        customer_phone=normalize_phone(node.get("phone") or ship.get("phone") or customer.get("phone")),
        items=[
            {
                "title": li["title"],
                "quantity": li["quantity"],
                "price": str(money(li.get("originalUnitPriceSet", {}).get("shopMoney", {}).get("amount"))),
            }
            for li in (node.get("lineItems") or {}).get("nodes", [])
        ],
        tracking=tracking,
        created_at=node.get("createdAt"),
    )


def _rest_order(o: dict) -> OrderData:
    tracking: dict = {}
    for f in o.get("fulfillments") or []:
        if f.get("tracking_number") or f.get("tracking_url"):
            tracking = {
                "courier": f.get("tracking_company"),
                "awb": f.get("tracking_number"),
                "url": f.get("tracking_url"),
                "status": f.get("shipment_status") or f.get("status"),
            }
    customer = o.get("customer") or {}
    ship = o.get("shipping_address") or {}
    name = ship.get("name") or " ".join(filter(None, [customer.get("first_name"), customer.get("last_name")])) or None
    status = "cancelled" if o.get("cancelled_at") else (o.get("fulfillment_status") or "unfulfilled")
    return OrderData(
        external_id=str(o["id"]),
        number=o.get("name") or str(o.get("order_number")),
        status=status,
        payment_method="cod" if _is_cod(o.get("payment_gateway_names") or []) else "prepaid",
        total=money(o.get("total_price")),
        currency=o.get("currency") or "INR",
        customer_name=name,
        customer_phone=normalize_phone(o.get("phone") or ship.get("phone") or customer.get("phone")),
        items=[{"title": li.get("title"), "quantity": li.get("quantity"), "price": str(money(li.get("price")))} for li in o.get("line_items") or []],
        tracking=tracking,
        created_at=o.get("created_at"),
    )


class ShopifyConnector(StoreConnector):
    kind = "shopify"

    def __init__(self, shop_url: str, access_token: str, api_secret: str, http: httpx.AsyncClient, storefront_url: str | None = None):
        self.shop_url = shop_url.rstrip("/")
        self.storefront_url = (storefront_url or self.shop_url).rstrip("/")
        self.endpoint = f"{self.shop_url}/admin/api/{API_VERSION}/graphql.json"
        self.headers = {"X-Shopify-Access-Token": access_token, "Content-Type": "application/json"}
        self.api_secret = api_secret
        self.http = http

    async def _gql(self, query: str, variables: dict | None = None) -> dict:
        try:
            resp = await self.http.post(self.endpoint, json={"query": query, "variables": variables or {}}, headers=self.headers, timeout=30)
        except httpx.HTTPError as exc:
            raise StoreError(f"Shopify unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise StoreError(f"Shopify {resp.status_code}: {resp.text[:300]}", permanent=resp.status_code in (401, 403, 404))
        data = resp.json()
        if data.get("errors"):
            raise StoreError(f"Shopify GraphQL error: {data['errors']}")
        return data["data"]

    @staticmethod
    def _user_errors(payload: dict) -> None:
        errors = payload.get("userErrors") or []
        if errors:
            raise StoreError("; ".join(e.get("message", "") for e in errors), permanent=True)

    async def iter_products(self) -> AsyncIterator[ProductData]:
        cursor = None
        while True:
            data = await self._gql(_PRODUCTS_QUERY, {"cursor": cursor})
            page = data["products"]
            for node in page["nodes"]:
                variants = [
                    VariantData(
                        id=str(v["legacyResourceId"]),
                        title=v["title"],
                        price=money(v["price"]),
                        in_stock=bool(v["availableForSale"]),
                        sku=v.get("sku"),
                    )
                    for v in node["variants"]["nodes"]
                ]
                first = node["variants"]["nodes"][0] if node["variants"]["nodes"] else {}
                image = ((node.get("featuredMedia") or {}).get("preview") or {}).get("image") or {}
                yield ProductData(
                    external_id=str(node["legacyResourceId"]),
                    title=node["title"],
                    description=node.get("description") or "",
                    price=money(first.get("price")),
                    compare_at_price=optional_money(first.get("compareAtPrice")),
                    url=node.get("onlineStoreUrl") or f"{self.storefront_url}/products/{node['handle']}",
                    image_url=image.get("url"),
                    in_stock=any(v.in_stock for v in variants),
                    stock=node.get("totalInventory"),
                    category=node.get("productType") or None,
                    tags=node.get("tags") or [],
                    variants=variants if len(variants) > 1 else [],
                )
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"]["endCursor"]

    async def _orders(self, q: str) -> list[OrderData]:
        data = await self._gql(_ORDERS_QUERY, {"q": q})
        return [_gql_order(n) for n in data["orders"]["nodes"]]

    async def get_order(self, ref: str) -> OrderData | None:
        ref = ref.strip()
        query = f"id:{ref}" if ref.isdigit() and len(ref) > 8 else f"name:#{ref.lstrip('#')}"
        orders = await self._orders(query)
        return orders[0] if orders else None

    async def find_orders_by_phone(self, phone: str) -> list[OrderData]:
        digits = normalize_phone(phone)
        return await self._orders(digits[-10:])

    async def _variant_gid(self, item: OrderItemInput) -> str:
        if item.variant_id:
            return f"gid://shopify/ProductVariant/{item.variant_id}"
        data = await self._gql(
            "query($id: ID!) { product(id: $id) { variants(first: 1) { nodes { legacyResourceId } } } }",
            {"id": f"gid://shopify/Product/{item.product_id}"},
        )
        nodes = ((data.get("product") or {}).get("variants") or {}).get("nodes") or []
        if not nodes:
            raise StoreError(f"Product {item.product_id} not found", permanent=True)
        return f"gid://shopify/ProductVariant/{nodes[0]['legacyResourceId']}"

    async def create_cod_order(self, customer: CustomerInput, items: list[OrderItemInput], note: str = "") -> OrderData:
        first, last = split_name(customer.name)
        phone = "+" + normalize_phone(customer.phone)
        line_items = [{"variantId": await self._variant_gid(i), "quantity": i.quantity} for i in items]
        order_input = {
            "lineItems": line_items,
            "financialStatus": "PENDING",
            "phone": phone,
            "note": note or "Order placed via WhatsApp assistant (Cash on Delivery)",
            "tags": ["whatsapp-bot", "COD"],
            "shippingAddress": {
                "firstName": first,
                "lastName": last,
                "address1": customer.address_line1,
                "address2": customer.address_line2,
                "city": customer.city,
                "province": customer.state,
                "zip": customer.pincode,
                "countryCode": "IN",
                "phone": phone,
            },
        }
        if customer.email:
            order_input["email"] = customer.email
        data = await self._gql(
            "mutation($order: OrderCreateOrderInput!) { orderCreate(order: $order) { order {"
            + _ORDER_FIELDS
            + "} userErrors { field message } } }",
            {"order": order_input},
        )
        self._user_errors(data["orderCreate"])
        order = _gql_order(data["orderCreate"]["order"])
        order.payment_method = "cod"
        return order

    async def create_checkout_link(self, items: list[OrderItemInput]) -> str:
        parts = []
        for item in items:
            gid = await self._variant_gid(item)
            parts.append(f"{gid.rsplit('/', 1)[1]}:{item.quantity}")
        return f"{self.storefront_url}/cart/{','.join(parts)}"

    async def confirm_cod_order(self, external_id: str) -> None:
        data = await self._gql(
            "mutation($id: ID!, $tags: [String!]!) { tagsAdd(id: $id, tags: $tags) { userErrors { field message } } }",
            {"id": f"gid://shopify/Order/{external_id}", "tags": ["COD-Confirmed"]},
        )
        self._user_errors(data["tagsAdd"])

    async def cancel_order(self, external_id: str, reason: str) -> None:
        data = await self._gql(
            "mutation($id: ID!, $note: String) { orderCancel(orderId: $id, reason: CUSTOMER, refund: false, restock: true,"
            " notifyCustomer: false, staffNote: $note) { orderCancelUserErrors { field message } } }",
            {"id": f"gid://shopify/Order/{external_id}", "note": reason[:255]},
        )
        errors = data["orderCancel"].get("orderCancelUserErrors") or []
        if errors:
            raise StoreError("; ".join(e["message"] for e in errors), permanent=True)

    def verify_webhook(self, headers: dict[str, str], body: bytes, path_with_query: str) -> bool:
        return verify_base64_hmac(body, headers.get("x-shopify-hmac-sha256"), self.api_secret)

    def parse_webhook(self, headers: dict[str, str], body: bytes) -> list[StoreEvent]:
        topic = headers.get("x-shopify-topic", "")
        event_id = headers.get("x-shopify-webhook-id") or headers.get("x-shopify-event-id") or ""
        data = json.loads(body)
        if topic in ("orders/create", "orders/updated", "orders/cancelled", "orders/fulfilled"):
            kind = "order.created" if topic == "orders/create" else "order.updated"
            return [StoreEvent(event_id=event_id or f"{topic}:{data.get('id')}:{data.get('updated_at')}", type=kind, order=_rest_order(data))]
        if topic in ("products/create", "products/update"):
            variants = data.get("variants") or []
            first = variants[0] if variants else {}
            images = data.get("images") or []
            image = (data.get("image") or {}).get("src") or (images[0]["src"] if images else None)

            def in_stock(v: dict) -> bool:
                return v.get("inventory_management") is None or v.get("inventory_policy") == "continue" or (v.get("inventory_quantity") or 0) > 0

            vdata = [VariantData(id=str(v["id"]), title=v.get("title", ""), price=money(v.get("price")), in_stock=in_stock(v), sku=v.get("sku")) for v in variants]
            product = ProductData(
                external_id=str(data["id"]),
                title=data.get("title", ""),
                description=strip_html(data.get("body_html")),
                price=money(first.get("price")),
                compare_at_price=optional_money(first.get("compare_at_price")),
                url=f"{self.storefront_url}/products/{data.get('handle')}",
                image_url=image,
                in_stock=any(v.in_stock for v in vdata) if vdata else True,
                stock=sum((v.get("inventory_quantity") or 0) for v in variants) if variants else None,
                category=data.get("product_type") or None,
                tags=[t.strip() for t in (data.get("tags") or "").split(",") if t.strip()],
                variants=vdata if len(vdata) > 1 else [],
                active=data.get("status", "active") == "active",
            )
            return [StoreEvent(event_id=event_id or f"{topic}:{data['id']}:{data.get('updated_at')}", type="product.updated", product=product, product_id=product.external_id)]
        if topic == "products/delete":
            return [StoreEvent(event_id=event_id or f"{topic}:{data.get('id')}", type="product.deleted", product_id=str(data.get("id")))]
        return []
