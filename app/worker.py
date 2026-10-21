"""Run the job worker as a separate process: python -m app.worker
(set RUN_WORKER_IN_PROCESS=false on the web process when you do this)."""

import asyncio
import logging
import signal

import app.pipeline  # noqa: F401 - registers job handlers
from app.config import get_settings
from app.connectors import close_http_client
from app.db import dispose_engine, session_factory
from app.jobs import worker_loop


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass
    try:
        await worker_loop(session_factory(), settings.worker_poll_seconds, stop)
    finally:
        await close_http_client()
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
