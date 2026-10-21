import base64
import hashlib
import hmac
import json
import time

import httpx
import respx

from app.connectors.base import CustomerInput, OrderItemInput, normalize_phone
from app.connectors.shopify import ShopifyConnector
from app.connectors.woocommerce import WooCommerceConnector
from app.security import sign_request, verify_meta_signature, verify_request_signature
from app.services.catalog import query_terms
from app.services.conversations import to_llm_history
from app.whatsapp.client import buttons_message, describe, template_message
from app.whatsapp.webhook import parse_webhook


def test_custom_signature_roundtrip_and_expiry():
    body = b'{"a":1}'
    headers = sign_request("s3cret", "POST", "/webhooks/store/x", body)
    assert verify_request_signature("s3cret", "POST", "/webhooks/store/x", body, headers["X-WA-Agent-Timestamp"], headers["X-WA-Agent-Signature"])
    assert not verify_request_signature("other", "POST", "/webhooks/store/x", body, headers["X-WA-Agent-Timestamp"], headers["X-WA-Agent-Signature"])
    assert not verify_request_signature("s3cret", "POST", "/webhooks/store/y", body, headers["X-WA-Agent-Timestamp"], headers["X-WA-Agent-Signature"])
    old = sign_request("s3cret", "POST", "/p", body, timestamp=int(time.time()) - 600)
    assert not verify_request_signature("s3cret", "POST", "/p", body, old["X-WA-Agent-Timestamp"], old["X-WA-Agent-Signature"])


def test_known_signature_vector():
    """Same vector is checked by integrations/nextjs/test/handler.test.ts."""
    headers = sign_request("test-secret", "GET", "/api/wa-agent/products?limit=100", b"", timestamp=1700000000)
    assert headers["X-WA-Agent-Signature"] == "sha256=db47e7cbb604b7cc29701deb31829065140b3c0a1840a8217b6aca4a27c19be3"


def test_meta_signature():
    body = b'{"x":1}'
    sig = "sha256=" + hmac.new(b"app", body, hashlib.sha256).hexdigest()
    assert verify_meta_signature(body, sig, "app")
    assert not verify_meta_signature(body, sig, "")
    assert not verify_meta_signature(body, None, "app")


def test_parse_webhook_variants():
    payload = {
        "entry": [{"changes": [{"field": "messages", "value": {
            "metadata": {"phone_number_id": "P"},
            "contacts": [{"wa_id": "91999", "profile": {"name": "Asha"}}],
            "messages": [
                {"from": "91999", "id": "m1", "timestamp": "1700000000", "type": "text", "text": {"body": "hi"},
                 "referral": {"source_type": "ad", "source_id": "123", "headline": "Diwali sale"}},
                {"from": "91999", "id": "m2", "timestamp": "1700000001", "type": "interactive",
                 "interactive": {"type": "button_reply", "button_reply": {"id": "buy:p1", "title": "Buy now"}}},
                {"from": "91999", "id": "m3", "timestamp": "1700000002", "type": "button", "button": {"payload": "cod:confirm:5", "text": "Confirm"}},
                {"from": "91999", "id": "m4", "timestamp": "1700000003", "type": "image", "image": {"id": "img"}},
            ],
            "statuses": [{"id": "out1", "status": "failed", "errors": [{"code": 131047, "title": "Re-engagement message"}]}],
        }}]}]
    }
    messages, statuses = parse_webhook(payload)
    assert [m.kind for m in messages] == ["text", "button", "button", "image"]
    assert messages[0].profile_name == "Asha" and messages[0].referral["headline"] == "Diwali sale"
    assert messages[1].button_payload == "buy:p1" and messages[2].button_payload == "cod:confirm:5"
    assert messages[3].text == "[customer sent a image]"
    assert statuses[0].status == "failed" and "131047" in statuses[0].error


def test_message_builders():
    tpl = template_message("cod", "en", ["A", "1"], ["cod:confirm:1", "cod:cancel:1"])
    assert tpl["template"]["components"][1] == {
        "type": "button", "sub_type": "quick_reply", "index": "0", "parameters": [{"type": "payload", "payload": "cod:confirm:1"}]
    }
    b = buttons_message("Pick", [("a", "A very long button title here"), ("b", "B")])
    assert b["interactive"]["action"]["buttons"][0]["reply"]["title"] == "A very long button t"
    assert describe(b) == "Pick\n[buttons: A very long button t | B]"


def test_phone_normalization():
    assert normalize_phone("+91 98765-43210") == "919876543210"
    assert normalize_phone("09876543210") == "919876543210"
    assert normalize_phone("9876543210") == "919876543210"
    assert normalize_phone("") == ""


def test_query_terms_drop_hinglish_filler():
    assert query_terms("mujhe blue bedsheet chahiye under 2000") == ["blue", "bedsheet"]
    assert query_terms("नीली चादर") == ["नीली", "चादर"]


def test_history_merges_turns_and_extracts_preamble():
    class M:
        def __init__(self, direction, body, sender="bot"):
            self.direction, self.body, self.sender, self.kind = direction, body, sender, "text"

    turns, preamble = to_llm_history([
        M("out", "[template cod_order_confirmation] Priya, 77"),
        M("in", "hi", "customer"), M("in", "order?", "customer"), M("out", "Sure"), M("out", "ok", "human"),
    ])
    assert preamble == ["[template cod_order_confirmation] Priya, 77"]
    assert turns == [{"role": "user", "content": "hi\norder?"}, {"role": "assistant", "content": "Sure\n(sent by a human team member) ok"}]


# --- Shopify ---


def _b64hmac(secret: str, body: bytes) -> str:
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


def test_shopify_webhook_order_and_product():
    conn = ShopifyConnector("https://demo.myshopify.com", "tok", "shh", httpx.AsyncClient(), storefront_url="https://demo.in")
    order = {
        "id": 555, "name": "#1042", "total_price": "1499.00", "currency": "INR", "payment_gateway_names": ["Cash on Delivery (COD)"],
        "phone": None, "shipping_address": {"name": "Kavya R", "phone": "+91 99887 76655"}, "customer": {},
        "line_items": [{"title": "Quilt", "quantity": 1, "price": "1499.00"}],
        "fulfillments": [{"tracking_company": "Bluedart", "tracking_number": "BD1", "tracking_url": "https://bd/1", "shipment_status": "in_transit"}],
        "created_at": "2026-10-01T10:00:00+05:30",
    }
    body = json.dumps(order).encode()
    headers = {"x-shopify-hmac-sha256": _b64hmac("shh", body), "x-shopify-topic": "orders/create", "x-shopify-webhook-id": "wh1"}
    assert conn.verify_webhook(headers, body, "/")
    assert not conn.verify_webhook({**headers, "x-shopify-hmac-sha256": "bad"}, body, "/")
    [event] = conn.parse_webhook(headers, body)
    o = event.order
    assert (event.type, o.number, o.payment_method, o.customer_phone, str(o.total)) == ("order.created", "#1042", "cod", "919988776655", "1499.00")
    assert o.tracking["awb"] == "BD1"

    product = {"id": 9, "title": "Razai", "handle": "razai", "body_html": "<p>Warm &amp; light</p>", "tags": "winter, quilt", "status": "active",
               "variants": [{"id": 91, "title": "Default", "price": "2799.00", "compare_at_price": "3499.00", "inventory_management": "shopify", "inventory_quantity": 0, "inventory_policy": "deny"}],
               "image": {"src": "https://cdn/razai.jpg"}}
    pbody = json.dumps(product).encode()
    [pevent] = conn.parse_webhook({"x-shopify-topic": "products/update"}, pbody)
    p = pevent.product
    assert (p.url, p.description, p.tags, p.in_stock, str(p.compare_at_price)) == ("https://demo.in/products/razai", "Warm & light", ["winter", "quilt"], False, "3499.00")


@respx.mock
async def test_shopify_checkout_link_and_cod_order():
    shop = "https://demo.myshopify.com"
    route = respx.post(f"{shop}/admin/api/2025-07/graphql.json")
    responses = [
        httpx.Response(200, json={"data": {"product": {"variants": {"nodes": [{"legacyResourceId": "4001"}]}}}}),
        httpx.Response(200, json={"data": {"orderCreate": {"userErrors": [], "order": {
            "legacyResourceId": "777", "name": "#1001", "createdAt": "2026-10-10T00:00:00Z", "cancelledAt": None,
            "displayFinancialStatus": "PENDING", "displayFulfillmentStatus": "UNFULFILLED", "paymentGatewayNames": [],
            "phone": "+919876543210", "totalPriceSet": {"shopMoney": {"amount": "999.0", "currencyCode": "INR"}},
            "customer": None, "shippingAddress": {"name": "Priya S", "phone": "+919876543210"},
            "lineItems": {"nodes": [{"title": "Sheet", "quantity": 1, "originalUnitPriceSet": {"shopMoney": {"amount": "999.0"}}}]},
            "fulfillments": [],
        }}}}),
    ]
    route.side_effect = responses
    async with httpx.AsyncClient() as http:
        conn = ShopifyConnector(shop, "tok", "shh", http)
        assert await conn.create_checkout_link([OrderItemInput(product_id="1", variant_id="4002", quantity=2)]) == f"{shop}/cart/4002:2"
        order = await conn.create_cod_order(
            CustomerInput(name="Priya S", phone="9876543210", address_line1="12 MI Road", city="Jaipur", state="Rajasthan", pincode="302001"),
            [OrderItemInput(product_id="1")],
        )
    assert order.number == "#1001" and order.payment_method == "cod"
    sent = json.loads(route.calls[1].request.content)
    assert sent["variables"]["order"]["lineItems"] == [{"variantId": "gid://shopify/ProductVariant/4001", "quantity": 1}]
    assert sent["variables"]["order"]["shippingAddress"]["zip"] == "302001"
    assert route.calls[1].request.headers["X-Shopify-Access-Token"] == "tok"


# --- WooCommerce ---


@respx.mock
async def test_woocommerce_cod_order_and_webhook():
    site = "https://shop.example.in"
    created = respx.post(f"{site}/wp-json/wc/v3/orders").mock(return_value=httpx.Response(201, json={
        "id": 321, "number": "321", "status": "processing", "payment_method": "cod", "total": "1798.00", "currency": "INR",
        "billing": {"first_name": "Amit", "last_name": "K", "phone": "+919812345678"}, "shipping": {},
        "line_items": [{"name": "Sheet", "quantity": 2, "price": 899}], "meta_data": [],
    }))
    async with httpx.AsyncClient() as http:
        conn = WooCommerceConnector(site, "ck", "cs", "whsec", http)
        order = await conn.create_cod_order(
            CustomerInput(name="Amit K", phone="9812345678", address_line1="5 Station Rd", city="Ajmer", state="RJ", pincode="305001"),
            [OrderItemInput(product_id="44", variant_id="45", quantity=2)],
        )
        assert await conn.create_checkout_link([OrderItemInput(product_id="44", quantity=1)]) == f"{site}/checkout/?add-to-cart=44&quantity=1"
    body = json.loads(created.calls[0].request.content)
    assert body["payment_method"] == "cod" and body["line_items"] == [{"product_id": 44, "quantity": 2, "variation_id": 45}]
    assert order.customer_phone == "919812345678" and order.payment_method == "cod"

    hook = json.dumps({"id": 321, "status": "completed", "payment_method": "cod", "total": "1798.00", "billing": {"phone": "9812345678"},
                       "meta_data": [{"key": "_wc_shipment_tracking_items", "value": [{"tracking_provider": "Delhivery", "tracking_number": "D1"}]}]}).encode()
    headers = {"x-wc-webhook-signature": _b64hmac("whsec", hook), "x-wc-webhook-topic": "order.updated", "x-wc-webhook-delivery-id": "d1"}
    conn = WooCommerceConnector(site, "ck", "cs", "whsec", httpx.AsyncClient())
    assert conn.verify_webhook(headers, hook, "/")
    [event] = conn.parse_webhook(headers, hook)
    assert event.order.tracking == {"courier": "Delhivery", "awb": "D1", "url": None, "status": "shipped"}
    assert conn.parse_webhook({"x-wc-webhook-topic": "order.created"}, b"webhook_id=5") == []


def test_settings_for_railway():
    from app.config import Settings

    s = Settings(database_url="postgresql://u:p@db.railway.internal:5432/railway", admin_token="x" * 32,
                 encryption_key="k", meta_app_secret="s", meta_verify_token="v", _env_file=None)
    assert s.database_url == "postgresql+asyncpg://u:p@db.railway.internal:5432/railway"
    assert s.problems() == []
    unsafe = Settings(admin_token="change-me", encryption_key="", meta_app_secret="", meta_verify_token="", _env_file=None)
    assert len(unsafe.problems()) == 4
