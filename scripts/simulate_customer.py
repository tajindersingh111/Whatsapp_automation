"""Send a fake WhatsApp message (signed like Meta does) to the local demo.

    .venv\\Scripts\\python -m scripts.simulate_customer "2000 ke under blue bedsheet?" --phone 919812345678 --name Asha
    .venv\\Scripts\\python -m scripts.simulate_customer --website-cod-order --phone 919812345678
    .venv\\Scripts\\python -m scripts.simulate_customer --tap confirm --phone 919812345678

--website-cod-order places a COD order on the mock store's "website", which makes the agent send
the COD confirmation template to that customer (visible in the dashboard inbox).
--tap confirm|cancel presses that template's button for the customer's latest pending COD order.
"""

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import time
import uuid

import httpx
from dotenv import dotenv_values

AGENT = "http://127.0.0.1:8000"
STORE = "http://127.0.0.1:8100"


def app_secret() -> str:
    return os.environ.get("META_APP_SECRET") or dotenv_values(".env").get("META_APP_SECRET") or "demo-app-secret"


async def latest_pending_cod_order(phone: str) -> int | None:
    from sqlalchemy import select

    from app.db import dispose_engine, session_factory
    from app.models import Order

    async with session_factory()() as session:
        order_id = await session.scalar(
            select(Order.id).where(Order.customer_phone == phone, Order.cod_status == "pending").order_by(Order.id.desc()).limit(1)
        )
    await dispose_engine()
    return order_id


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("text", nargs="?", default="")
    p.add_argument("--phone", default="919812345678")
    p.add_argument("--name", default="Asha")
    p.add_argument("--button", help="button payload, e.g. cod:confirm:1")
    p.add_argument("--website-cod-order", action="store_true")
    p.add_argument("--tap", choices=["confirm", "cancel"], help="press a COD template button")
    args = p.parse_args()

    if args.tap:
        order_id = asyncio.run(latest_pending_cod_order(args.phone))
        if order_id is None:
            raise SystemExit("No pending COD order for this phone")
        args.button = f"cod:{args.tap}:{order_id}"
        args.text = args.text or ("Confirm order" if args.tap == "confirm" else "Cancel order")

    if args.website_cod_order:
        r = httpx.post(f"{STORE}/_demo/website-order", json={"phone": args.phone, "name": args.name})
        print(r.status_code, r.json()["order"]["number"])
        return

    message: dict = {"from": args.phone, "id": f"wamid.sim.{uuid.uuid4().hex}", "timestamp": str(int(time.time()))}
    if args.button:
        message.update({"type": "button", "button": {"payload": args.button, "text": args.text or "OK"}})
    else:
        message.update({"type": "text", "text": {"body": args.text}})
    payload = {
        "object": "whatsapp_business_account",
        "entry": [{"id": "demo", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": "sandbox", "phone_number_id": "sandbox-demo"},
            "contacts": [{"profile": {"name": args.name}, "wa_id": args.phone}],
            "messages": [message],
        }}]}],
    }
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(app_secret().encode(), body, hashlib.sha256).hexdigest()
    r = httpx.post(f"{AGENT}/webhooks/meta", content=body, headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})
    print(r.status_code, r.text)


if __name__ == "__main__":
    main()
