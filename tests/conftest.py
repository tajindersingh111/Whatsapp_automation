import os

from cryptography.fernet import Fernet

os.environ.update(
    {
        "DATABASE_URL": os.environ.get("TEST_DATABASE_URL", "postgresql+asyncpg://postgres@localhost:5436/wa_agent_test"),
        "ADMIN_TOKEN": "test-admin",
        "ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "META_APP_SECRET": "test-app-secret",
        "META_VERIFY_TOKEN": "verify-me",
        "PUBLIC_BASE_URL": "http://agent.test",
        "INBOUND_DEBOUNCE_SECONDS": "0",
        "RUN_WORKER_IN_PROCESS": "false",
        "ANTHROPIC_API_KEY": "test-not-used",
    }
)

import hashlib  # noqa: E402
import hmac  # noqa: E402
import json  # noqa: E402
import time  # noqa: E402
from typing import Any  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
from anthropic.types.beta import BetaMessage  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from sqlalchemy import text  # noqa: E402

import app.connectors as connectors_mod  # noqa: E402
from app.agent import llm as llm_mod  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import Base, get_engine, session_factory  # noqa: E402
from app.jobs import run_pending  # noqa: E402
from app.main import app as agent_app  # noqa: E402
from app.models import StoreConnection, Tenant, WhatsAppAccount  # noqa: E402
from app.security import encrypt, encrypt_json, sign_request  # noqa: E402
from scripts.mock_store import create_mock_store  # noqa: E402

get_settings.cache_clear()

STORE_SECRET = "store-secret"
STORE_BASE = "http://store.test/api/wa-agent"
PHONE_NUMBER_ID = "1111"
CUSTOMER = "919876543210"


# ---------- fake Claude ----------


def msg(*blocks: dict, stop_reason: str = "end_turn") -> BetaMessage:
    return BetaMessage.model_validate(
        {
            "id": f"msg_{time.time_ns()}",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5-5",
            "content": list(blocks),
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        }
    )


def text_block(t: str) -> dict:
    return {"type": "text", "text": t}


def tool_block(name: str, inp: dict, id: str | None = None) -> dict:
    return {"type": "tool_use", "id": id or f"toolu_{name}_{time.time_ns()}", "name": name, "input": inp}


class FakeLLM:
    def __init__(self, script: list[BetaMessage] | None = None):
        self.script = list(script or [])
        self.calls: list[dict[str, Any]] = []

    async def create(self, **params: Any) -> BetaMessage:
        # Snapshot: the agent keeps appending to the same messages list.
        self.calls.append({**params, "messages": list(params.get("messages", []))})
        if not self.script:
            return msg(text_block("(fake default reply)"))
        return self.script.pop(0)

    def tool_results(self, call_index: int) -> list[dict]:
        last = self.calls[call_index]["messages"][-1]
        return [b for b in last["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]


@pytest.fixture
def fake_llm():
    fake = FakeLLM()
    llm_mod.set_llm(fake)
    yield fake
    llm_mod.set_llm(None)


# ---------- fake Meta Graph API ----------


class GraphFake:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.read: list[str] = []
        self.fail_with: dict | None = None
        self.fail_status = 400
        self.app = FastAPI()

        @self.app.post("/{version}/{phone_id}/messages")
        async def messages(version: str, phone_id: str, request: Request):
            body = await request.json()
            if body.get("status") == "read":
                self.read.append(body["message_id"])
                return {"success": True}
            if self.fail_with:
                from fastapi.responses import JSONResponse

                return JSONResponse(status_code=self.fail_status, content={"error": self.fail_with})
            if self.fail_after is not None and len(self.sent) >= self.fail_after:
                from fastapi.responses import JSONResponse

                return JSONResponse(status_code=400, content={"error": {"message": "Rate limit hit", "code": 130429}})
            self.sent.append(body)
            return {"messages": [{"id": f"wamid.out.{len(self.sent)}"}]}

        @self.app.get("/{version}/{waba_id}/message_templates")
        async def templates(version: str, waba_id: str):
            return {"data": self.templates}

        @self.app.get("/{version}/{node_id}")
        async def phone_info(version: str, node_id: str, request: Request):
            if request.headers.get("authorization") == "Bearer expired":
                from fastapi.responses import JSONResponse

                return JSONResponse(status_code=401, content={"error": {"message": "Session has expired", "code": 190}})
            return {"id": node_id, "display_phone_number": "+91 90000 00000", "verified_name": "Jaipur Linen Co",
                    "quality_rating": "GREEN", "name_status": "APPROVED", "platform_type": "CLOUD_API"}

        @self.app.post("/{version}/{waba_id}/subscribed_apps")
        async def subscribe(version: str, waba_id: str):
            self.subscribed.append(waba_id)
            return {"success": True}

        self.fail_after: int | None = None
        self.templates: list[dict] = []
        self.subscribed: list[str] = []


@pytest.fixture
def graph():
    return GraphFake()


@pytest.fixture
def mock_store():
    return create_mock_store(STORE_SECRET, agent_webhook_url=None)


@pytest.fixture
async def http(graph, mock_store):
    client = httpx.AsyncClient(
        mounts={
            "https://graph.facebook.com": httpx.ASGITransport(app=graph.app),
            "http://store.test": httpx.ASGITransport(app=mock_store),
        }
    )
    connectors_mod._http = client
    yield client
    connectors_mod._http = None
    await client.aclose()


# ---------- database ----------


@pytest.fixture(scope="session")
async def schema():
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield


@pytest.fixture
async def db(schema):
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with get_engine().begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    async with session_factory()() as session:
        yield session


@pytest.fixture
async def tenant(db, http) -> Tenant:
    t = Tenant(
        name="Jaipur Linen Co",
        slug="jaipur-linen",
        config={
            "brand_name": "Jaipur Linen Co",
            "reply_mode": "ai",
            "about": "Hand block printed cotton bedding from Jaipur.",
            "rules": ["Never promise delivery dates."],
            "offers": [{"code": "FIRST10", "description": "10% off first order above ₹999"}],
            "lead_followup": {"enabled": True, "after_hours": 3, "max_followups": 1},
        },
    )
    db.add(t)
    await db.flush()
    db.add(WhatsAppAccount(tenant_id=t.id, phone_number_id=PHONE_NUMBER_ID, access_token_enc=encrypt("EAAG-test-token"), display_phone="+91 90000 00000"))
    db.add(StoreConnection(tenant_id=t.id, kind="custom", base_url=STORE_BASE, credentials_enc=encrypt_json({"secret": STORE_SECRET})))
    await db.commit()
    return t


@pytest.fixture
async def api(http):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=agent_app), base_url="http://agent.test") as client:
        yield client


ADMIN = {"Authorization": "Bearer test-admin"}


# ---------- helpers ----------


def meta_payload(text_body: str | None = None, *, wamid: str, wa_id: str = CUSTOMER, button: tuple[str, str] | None = None, name: str = "Priya") -> dict:
    message: dict = {"from": wa_id, "id": wamid, "timestamp": str(int(time.time()))}
    if button:
        message.update({"type": "button", "button": {"payload": button[0], "text": button[1]}})
    else:
        message.update({"type": "text", "text": {"body": text_body}})
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"display_phone_number": "919000000000", "phone_number_id": PHONE_NUMBER_ID},
                            "contacts": [{"profile": {"name": name}, "wa_id": wa_id}],
                            "messages": [message],
                        },
                    }
                ],
            }
        ],
    }


async def post_meta(api: httpx.AsyncClient, payload: dict) -> httpx.Response:
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(b"test-app-secret", body, hashlib.sha256).hexdigest()
    return await api.post("/webhooks/meta", content=body, headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})


async def post_store_event(api: httpx.AsyncClient, tenant_id, event: dict) -> httpx.Response:
    body = json.dumps(event).encode()
    path = f"/webhooks/store/{tenant_id}"
    return await api.post(path, content=body, headers={**sign_request(STORE_SECRET, "POST", path, body), "Content-Type": "application/json"})


async def drain() -> int:
    return await run_pending(session_factory())
