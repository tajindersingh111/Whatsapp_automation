import base64
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.connectors import build_connector
from app.db import get_session
from app.jobs import enqueue
from app.models import CampaignRecipient, Message, StoreConnection
from app.pipeline import serialize_inbound
from app.security import verify_meta_signature
from app.whatsapp.webhook import parse_webhook

log = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])

_STATUS_RANK = {"queued": 0, "simulated": 0, "sent": 1, "delivered": 2, "read": 3, "failed": 4}


@router.get("/meta")
async def meta_verify(
    mode: str = Query(alias="hub.mode", default=""),
    token: str = Query(alias="hub.verify_token", default=""),
    challenge: str = Query(alias="hub.challenge", default=""),
) -> Response:
    if mode == "subscribe" and token == get_settings().meta_verify_token:
        return Response(content=challenge, media_type="text/plain")
    raise HTTPException(status_code=403, detail="verification failed")


@router.post("/meta")
async def meta_webhook(request: Request, session: AsyncSession = Depends(get_session)) -> dict:
    body = await request.body()
    if not verify_meta_signature(body, request.headers.get("x-hub-signature-256"), get_settings().meta_app_secret):
        raise HTTPException(status_code=401, detail="bad signature")
    messages, statuses = parse_webhook(await request.json())
    for m in messages:
        await enqueue(session, "inbound_message", serialize_inbound(m), dedupe_key=f"wamid:{m.wa_message_id}"[:200], max_attempts=5)
    for st in statuses:
        recipient = await session.scalar(select(CampaignRecipient).where(CampaignRecipient.wa_message_id == st.wa_message_id))
        if recipient is not None and _STATUS_RANK.get(st.status, 0) >= _STATUS_RANK.get(recipient.status, 0):
            recipient.status = st.status
            if st.error:
                recipient.error = st.error
        current = await session.scalar(select(Message).where(Message.wa_message_id == st.wa_message_id))
        if current is None:
            continue
        if _STATUS_RANK.get(st.status, 0) >= _STATUS_RANK.get(current.status, 0):
            await session.execute(
                update(Message).where(Message.id == current.id).values(status=st.status, error=st.error or current.error)
            )
    await session.commit()
    return {"ok": True}


@router.post("/store/{tenant_id}")
async def store_webhook(tenant_id: uuid.UUID, request: Request, session: AsyncSession = Depends(get_session)) -> dict:
    conn = await session.scalar(select(StoreConnection).where(StoreConnection.tenant_id == tenant_id))
    if conn is None:
        raise HTTPException(status_code=404, detail="unknown store")
    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    if not build_connector(conn).verify_webhook(headers, body, path):
        raise HTTPException(status_code=401, detail="bad signature")
    keep = {k: v for k, v in headers.items() if k.startswith(("x-shopify-", "x-wc-", "x-wa-agent-"))}
    await enqueue(
        session,
        "store_webhook",
        {"tenant_id": str(tenant_id), "headers": keep, "body_b64": base64.b64encode(body).decode()},
        tenant_id=tenant_id,
    )
    await session.commit()
    return {"ok": True}
