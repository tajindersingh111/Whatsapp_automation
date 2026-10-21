"""Local demo: agent on :8000 + mock store on :8100 + a seeded demo brand.

    .venv\\Scripts\\python scripts\\run_demo.py

WhatsApp sending is sandboxed (phone_number_id "sandbox-demo"): replies show up in the
dashboard inbox but nothing is sent to Meta. Simulate customers with scripts/simulate_customer.py.
"""

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.stdout.reconfigure(line_buffering=True)
sys.path.insert(0, str(ROOT))

from dotenv import dotenv_values  # noqa: E402

_env = dotenv_values(ROOT / ".env")
DEMO_META_SECRET = "demo-app-secret"
if not (os.environ.get("META_APP_SECRET") or _env.get("META_APP_SECRET")):
    os.environ["META_APP_SECRET"] = DEMO_META_SECRET

import uvicorn  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import session_factory  # noqa: E402
from app.jobs import enqueue  # noqa: E402
from app.models import KnowledgeDoc, Order, StoreConnection, Tenant, WhatsAppAccount  # noqa: E402
from app.security import encrypt, encrypt_json  # noqa: E402
from scripts.mock_store import create_mock_store  # noqa: E402

STORE_SECRET = "demo-store-secret"
SLUG = "demo-jaipur-linen"

DEMO_CONFIG = {
    "brand_name": "Jaipur Linen Co (demo)",
    "assistant_name": "Riya",
    "store_url": "https://shop.example.com",
    "about": "Hand block printed cotton bedding, quilts and cushion covers made by artisans in Jaipur and Bagru. Customers buy for their own homes and as wedding/housewarming gifts.",
    "rules": ["Never promise an exact delivery date; share the typical range from the shipping policy.", "Suggest gift wrapping when the customer mentions a gift."],
    "offers": [{"code": "FIRST10", "description": "10% off on the first order above ₹999"}],
    "support_hours": "10am-7pm, Monday to Saturday",
    "lead_followup": {"enabled": True, "after_hours": 3, "max_followups": 1},
}

DEMO_KNOWLEDGE = [
    ("Shipping & delivery", "Free shipping on orders above ₹999, otherwise ₹79. Orders ship in 1-2 working days from Jaipur. Delivery takes 3-5 days in metros and 5-8 days elsewhere."),
    ("Cash on Delivery", "COD is available on orders up to ₹5,000 across India. A COD order must be confirmed on WhatsApp before it ships."),
    ("Returns & exchange", "7-day easy returns for unused items in original packaging. Refunds go to the original payment method or as store credit for COD orders, within 5 working days of pickup."),
    ("Care", "Hand block prints use natural dyes. Wash separately in cold water with mild detergent for the first 2 washes; dry in shade."),
]


async def next_order_number(tenant_id: str) -> int:
    """The mock store forgets its orders on restart; continue numbering after what the agent has seen."""
    async with session_factory()() as session:
        numbers = (await session.scalars(select(Order.number).where(Order.tenant_id == tenant_id))).all()
    return max([int(n) for n in numbers if n.isdigit()] + [1000]) + 1


async def seed() -> str:
    async with session_factory()() as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.slug == SLUG))
        if tenant is None:
            tenant = Tenant(name="Jaipur Linen Co (demo)", slug=SLUG, config=DEMO_CONFIG)
            session.add(tenant)
            await session.flush()
            for title, content in DEMO_KNOWLEDGE:
                session.add(KnowledgeDoc(tenant_id=tenant.id, title=title, content=content))
            session.add(WhatsAppAccount(tenant_id=tenant.id, phone_number_id="sandbox-demo", display_phone="sandbox", access_token_enc=encrypt("sandbox")))
            session.add(StoreConnection(
                tenant_id=tenant.id, kind="custom", base_url="http://127.0.0.1:8100/api/wa-agent",
                credentials_enc=encrypt_json({"secret": STORE_SECRET}),
            ))
        else:
            # Keep the demo store and agent agreeing on the secret even if it was changed from the dashboard.
            conn = await session.scalar(select(StoreConnection).where(StoreConnection.tenant_id == tenant.id))
            conn.credentials_enc = encrypt_json({"secret": STORE_SECRET})
        await enqueue(session, "sync_catalog", {"tenant_id": str(tenant.id)}, tenant_id=tenant.id, delay_seconds=2)
        await session.commit()
        return str(tenant.id)


async def main() -> None:
    tenant_id = await seed()
    store = create_mock_store(
        STORE_SECRET,
        agent_webhook_url=f"http://127.0.0.1:8000/webhooks/store/{tenant_id}",
        first_order_number=await next_order_number(tenant_id),
    )
    servers = [
        uvicorn.Server(uvicorn.Config("app.main:app", host="127.0.0.1", port=8000, log_level="info")),
        uvicorn.Server(uvicorn.Config(store, host="127.0.0.1", port=8100, log_level="warning")),
    ]
    print(f"\nDemo brand {tenant_id}\nDashboard: http://127.0.0.1:8000/dashboard  (token = ADMIN_TOKEN in .env)\n")
    await asyncio.gather(*(s.serve() for s in servers))


if __name__ == "__main__":
    asyncio.run(main())
