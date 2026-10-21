"""Tiny Postgres job queue.

Webhooks only validate + enqueue and return 200 immediately; the worker does the slow work
(LLM calls, store API calls, WhatsApp sends). Jobs retry with backoff and can be deduplicated.
"""

import asyncio
import logging
import traceback
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Job

log = logging.getLogger(__name__)

Handler = Callable[[AsyncSession, Job], Awaitable[None]]
_handlers: dict[str, Handler] = {}

STALE_LOCK = timedelta(minutes=10)


def handler(kind: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        _handlers[kind] = fn
        return fn

    return register


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def enqueue(
    session: AsyncSession,
    kind: str,
    payload: dict | None = None,
    *,
    tenant_id: uuid.UUID | None = None,
    delay_seconds: float = 0,
    run_at: datetime | None = None,
    dedupe_key: str | None = None,
    max_attempts: int = 5,
) -> None:
    """Add a job in the caller's transaction (commit is the caller's job)."""
    stmt = insert(Job).values(
        kind=kind,
        payload=payload or {},
        tenant_id=tenant_id,
        run_at=run_at or (utcnow() + timedelta(seconds=delay_seconds)),
        dedupe_key=dedupe_key,
        max_attempts=max_attempts,
    )
    if dedupe_key:
        stmt = stmt.on_conflict_do_nothing(index_elements=["dedupe_key"])
    await session.execute(stmt)


_CLAIM_SQL = text(
    """
    UPDATE jobs SET status = 'running', attempts = attempts + 1, locked_at = now()
    WHERE id = (
        SELECT id FROM jobs
        WHERE status = 'queued' AND run_at <= now()
        ORDER BY run_at, id
        LIMIT 1
        FOR UPDATE SKIP LOCKED
    )
    RETURNING id
    """
)


async def _claim(sessions: async_sessionmaker[AsyncSession]) -> int | None:
    async with sessions() as session:
        job_id = (await session.execute(_CLAIM_SQL)).scalar_one_or_none()
        await session.commit()
        return job_id


async def _run(sessions: async_sessionmaker[AsyncSession], job_id: int) -> None:
    async with sessions() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        fn = _handlers.get(job.kind)
        try:
            if fn is None:
                raise RuntimeError(f"No handler registered for job kind {job.kind!r}")
            await fn(session, job)
            job.status = "done"
            job.finished_at = utcnow()
            job.last_error = None
            await session.commit()
        except Exception as exc:  # noqa: BLE001 - every failure is recorded and retried
            await session.rollback()
            job = await session.get(Job, job_id)
            assert job is not None
            job.last_error = "".join(traceback.format_exception_only(exc)).strip()[:4000]
            if job.attempts >= job.max_attempts or isinstance(exc, PermanentJobError):
                job.status = "failed"
                job.finished_at = utcnow()
                log.error("job %s (%s) failed permanently: %s", job.id, job.kind, job.last_error)
            else:
                job.status = "queued"
                job.run_at = utcnow() + timedelta(seconds=min(30 * 2 ** (job.attempts - 1), 3600))
                log.warning("job %s (%s) failed, retrying: %s", job.id, job.kind, job.last_error)
            await session.commit()


class PermanentJobError(Exception):
    """Raise from a handler when retrying cannot help (bad config, invalid input)."""


async def requeue_stale(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session:
        await session.execute(
            update(Job)
            .where(Job.status == "running", Job.locked_at < utcnow() - STALE_LOCK)
            .values(status="queued")
        )
        await session.commit()


async def run_pending(sessions: async_sessionmaker[AsyncSession], limit: int = 100) -> int:
    """Run ready jobs until none are left (or limit). Used by tests and the CLI."""
    count = 0
    while count < limit:
        job_id = await _claim(sessions)
        if job_id is None:
            break
        await _run(sessions, job_id)
        count += 1
    return count


async def worker_loop(sessions: async_sessionmaker[AsyncSession], poll_seconds: float, stop: asyncio.Event) -> None:
    log.info("job worker started")
    last_reap = 0.0
    loop = asyncio.get_running_loop()
    while not stop.is_set():
        try:
            if loop.time() - last_reap > 60:
                await requeue_stale(sessions)
                last_reap = loop.time()
            ran = await run_pending(sessions, limit=20)
        except Exception:  # noqa: BLE001 - keep the worker alive on DB hiccups
            log.exception("worker iteration failed")
            ran = 0
        if ran == 0:
            try:
                await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
            except TimeoutError:
                pass
    log.info("job worker stopped")


async def pending_jobs(session: AsyncSession, kind: str | None = None) -> list[Job]:
    stmt = select(Job).where(Job.status == "queued").order_by(Job.id)
    if kind:
        stmt = stmt.where(Job.kind == kind)
    return list((await session.scalars(stmt)).all())
