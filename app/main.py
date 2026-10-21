import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse

import app.pipeline  # noqa: F401 - registers job handlers
from app.api import admin, webhooks
from app.config import get_settings
from app.connectors import close_http_client
from app.db import dispose_engine, session_factory
from app.jobs import worker_loop

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("app")
    for issue in settings.problems():
        log.warning("config: %s", issue)
    if settings.environment == "production" and settings.problems():
        raise RuntimeError("Refusing to start in production: " + "; ".join(settings.problems()))
    stop = asyncio.Event()
    worker = None
    if settings.run_worker_in_process:
        worker = asyncio.create_task(worker_loop(session_factory(), settings.worker_poll_seconds, stop))
    yield
    stop.set()
    if worker:
        await worker
    await close_http_client()
    await dispose_engine()


app = FastAPI(title="WhatsApp Sales Agent", version="0.1.0", lifespan=lifespan)
app.include_router(webhooks.router)
app.include_router(admin.router)


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse("/dashboard")


@app.get("/dashboard", include_in_schema=False)
async def dashboard() -> FileResponse:
    return FileResponse(STATIC / "dashboard.html")
