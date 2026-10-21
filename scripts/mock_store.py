"""A fake store that speaks the WA Agent Store API v1 (same contract as integrations/nextjs).

Run:  python -m scripts.mock_store            (serves on :8100, secret from MOCK_STORE_SECRET)
Used by the tests (in-process) and for demos without a real store.
"""

import itertools
import json
import os
import time
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException, Request

from app.security import sign_request, verify_request_signature

SAMPLE_PRODUCTS = [
    {
        "id": "p1", "title": "Indigo Dabu Hand Block Print Double Bedsheet", "price": 1899, "compare_at_price": 2499,
        "description": "Pure cotton king size bedsheet (108x108 in) with 2 pillow covers. Natural indigo dabu print from Bagru, Jaipur.",
        "category": "Bedsheets", "tags": ["cotton", "indigo", "blue", "king size", "hand block"], "in_stock": True, "stock": 14,
        "image_url": "https://picsum.photos/seed/indigo-bedsheet/800/800",
    },
    {
        "id": "p2", "title": "Sanganeri Floral Cotton Single Bedsheet", "price": 899, "compare_at_price": 1199,
        "description": "Soft 144 TC cotton single bedsheet (60x90 in) with 1 pillow cover. Pink and white floral Sanganeri print.",
        "category": "Bedsheets", "tags": ["cotton", "pink", "floral", "single"], "in_stock": True, "stock": 30,
        "image_url": "https://picsum.photos/seed/floral-bedsheet/800/800",
    },
    {
        "id": "p3", "title": "Mirror Work Cushion Cover Set of 5", "price": 1199, "compare_at_price": None,
        "description": "16x16 in cotton cushion covers with hand-done mirror work. Mixed jewel tones.",
        "category": "Cushion Covers", "tags": ["cushion", "mirror work", "gift"], "in_stock": True, "stock": 8,
        "image_url": "https://picsum.photos/seed/cushions/800/800",
        "variants": [
            {"id": "p3-red", "title": "Red & Orange", "price": 1199, "in_stock": True},
            {"id": "p3-blue", "title": "Blue & Teal", "price": 1199, "in_stock": False},
        ],
    },
    {
        "id": "p4", "title": "Jaipuri Razai Quilt (Double)", "price": 2799, "compare_at_price": 3499,
        "description": "Lightweight hand-quilted cotton razai, 90x108 in, filled with cotton. Ideal for Jaipur winters and AC rooms.",
        "category": "Quilts", "tags": ["razai", "quilt", "winter", "double"], "in_stock": False, "stock": 0,
        "image_url": "https://picsum.photos/seed/razai/800/800",
    },
]


def create_mock_store(secret: str, agent_webhook_url: str | None = None, base_path: str = "/api/wa-agent", first_order_number: int = 1001) -> FastAPI:
    app = FastAPI(title="Mock store")
    products = {p["id"]: {**p, "url": f"https://shop.example.com/products/{p['id']}", "currency": "INR"} for p in SAMPLE_PRODUCTS}
    orders: dict[str, dict] = {}
    counter = itertools.count(first_order_number)
    app.state.orders = orders
    app.state.calls = []

    async def verified(request: Request) -> bytes:
        body = await request.body()
        path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        ok = verify_request_signature(
            secret, request.method, path, body,
            request.headers.get("x-wa-agent-timestamp"), request.headers.get("x-wa-agent-signature"),
        )
        if not ok:
            raise HTTPException(status_code=401, detail="bad signature")
        app.state.calls.append((request.method, path))
        return body

    async def notify(event_type: str, order: dict) -> None:
        if not agent_webhook_url:
            return
        body = json.dumps({"event_id": f"{event_type}:{order['id']}:{time.time_ns()}", "type": event_type, "order": order}).encode()
        path = httpx.URL(agent_webhook_url).raw_path.decode()
        headers = {**sign_request(secret, "POST", path, body), "Content-Type": "application/json"}
        async with httpx.AsyncClient() as client:
            await client.post(agent_webhook_url, content=body, headers=headers, timeout=10)

    @app.get(f"{base_path}/products")
    async def list_products(request: Request, cursor: str | None = None, limit: int = 100):
        await verified(request)
        items = list(products.values())
        start = int(cursor or 0)
        page = items[start:start + limit]
        nxt = str(start + limit) if start + limit < len(items) else None
        return {"products": page, "next_cursor": nxt}

    @app.get(f"{base_path}/orders/{{ref}}")
    async def get_order(ref: str, request: Request):
        await verified(request)
        order = orders.get(ref) or next((o for o in orders.values() if o["number"] == ref), None)
        if order is None:
            raise HTTPException(status_code=404, detail="not found")
        return {"order": order}

    @app.get(f"{base_path}/orders")
    async def find_orders(request: Request, phone: str = ""):
        await verified(request)
        found = [o for o in orders.values() if o["customer"]["phone"][-10:] == phone[-10:]]
        return {"orders": sorted(found, key=lambda o: o["created_at"], reverse=True)[:5]}

    def build_order(customer: dict, items: list[dict], payment_method: str) -> dict:
        lines, total = [], 0
        for it in items:
            p = products.get(it["product_id"])
            if p is None:
                raise HTTPException(status_code=422, detail=f"unknown product {it['product_id']}")
            title, price = p["title"], p["price"]
            if it.get("variant_id"):
                v = next((v for v in p.get("variants", []) if v["id"] == it["variant_id"]), None)
                if v is None:
                    raise HTTPException(status_code=422, detail="unknown variant")
                title, price = f"{title} - {v['title']}", v["price"]
            lines.append({"title": title, "quantity": it.get("quantity", 1), "price": price})
            total += price * it.get("quantity", 1)
        number = str(next(counter))
        return {
            "id": f"ord_{number}", "number": number, "status": "pending", "payment_method": payment_method,
            "total": total, "currency": "INR", "customer": {"name": customer["name"], "phone": customer["phone"]},
            "items": lines, "tracking": None, "created_at": datetime.now(timezone.utc).isoformat(),
        }

    @app.post(f"{base_path}/orders")
    async def create_order(request: Request):
        data = json.loads(await verified(request))
        order = build_order(data["customer"], data["items"], data.get("payment_method", "cod"))
        order["status"] = "confirmed"
        orders[order["id"]] = order
        return {"order": order}

    @app.post(f"{base_path}/checkout-links")
    async def checkout_link(request: Request):
        data = json.loads(await verified(request))
        ids = ",".join(f"{i.get('variant_id') or i['product_id']}:{i.get('quantity', 1)}" for i in data["items"])
        return {"url": f"https://shop.example.com/cart?items={ids}"}

    @app.post(f"{base_path}/orders/{{order_id}}/confirm-cod")
    async def confirm(order_id: str, request: Request):
        await verified(request)
        if order_id not in orders:
            raise HTTPException(status_code=404)
        orders[order_id]["status"] = "confirmed"
        return {"order": orders[order_id]}

    @app.post(f"{base_path}/orders/{{order_id}}/cancel")
    async def cancel(order_id: str, request: Request):
        await verified(request)
        if order_id not in orders:
            raise HTTPException(status_code=404)
        orders[order_id]["status"] = "cancelled"
        return {"order": orders[order_id]}

    # --- demo helpers (unsigned, for local testing only) ---

    @app.post("/_demo/website-order")
    async def demo_website_order(payload: dict):
        """Simulate a customer placing a COD order on the website -> agent sends a confirmation template."""
        order = build_order(
            {"name": payload.get("name", "Priya Sharma"), "phone": payload["phone"]},
            payload.get("items") or [{"product_id": "p1", "quantity": 1}],
            "cod",
        )
        orders[order["id"]] = order
        await notify("order.created", order)
        return {"order": order}

    @app.post("/_demo/ship/{order_id}")
    async def demo_ship(order_id: str):
        order = orders[order_id]
        order["status"] = "shipped"
        order["tracking"] = {"courier": "Delhivery", "awb": f"AWB{order['number']}", "url": f"https://track.example.com/{order['number']}", "status": "in_transit"}
        await notify("order.updated", order)
        return {"order": order}

    return app


app = create_mock_store(
    os.environ.get("MOCK_STORE_SECRET", "dev-secret"),
    os.environ.get("AGENT_WEBHOOK_URL"),
)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8100)
