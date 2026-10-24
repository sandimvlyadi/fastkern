"""Fire-and-forget writes/purges for `sys_api_request_logs`, off the request path.

The request-log write also moves the key's `last_used_at` forward. It lives here rather than
in the request because an UPDATE there would lock the key's row until the request commits,
queueing every concurrent request made with the same key.

Mirrors `app.modules.audit_log.infrastructure.tasks` (the write) and
`app.modules.files.infrastructure.tasks` (the beat-scheduled purge) exactly.
This table has its own short retention, separate from `sys_audit_logs`,
because it is high-volume observability/billing-support data, not the
compliance-grade audit trail.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import delete, or_, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.celery_app import celery_app
from app.core.config import get_settings
from app.core.db import TRANSIENT_DB_ERRORS, create_engine
from app.core.logging import get_logger
from app.modules.api_clients.infrastructure.models import ApiKeyModel, ApiRequestLogModel

logger = get_logger(__name__)

# `last_used_at` is moved only once it is this stale, so a busy key costs one UPDATE a minute
# rather than one per request (each also fires the `updated_at` trigger).
_LAST_USED_RESOLUTION = timedelta(minutes=1)


async def _record(payload: dict[str, Any]) -> None:
    # A task-local engine, not the shared `app.core.db` one — see
    # `audit_log.infrastructure.tasks._record` for why (asyncpg pool bound to
    # one event loop; a Celery task owns its own loop via `asyncio.run()`).
    api_key_id = UUID(payload["api_key_id"])
    # Absent from a message queued before `used_at` was added.
    raw_used_at = payload.get("used_at")
    used_at = datetime.fromisoformat(raw_used_at) if raw_used_at else datetime.now(UTC)
    engine = create_engine(get_settings(), pooled=False)
    try:
        session_factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
        async with session_factory() as session:
            await session.execute(
                update(ApiKeyModel)
                .where(
                    ApiKeyModel.id == api_key_id,
                    or_(
                        ApiKeyModel.last_used_at.is_(None),
                        ApiKeyModel.last_used_at < used_at - _LAST_USED_RESOLUTION,
                    ),
                )
                .values(last_used_at=used_at)
            )
            session.add(
                ApiRequestLogModel(
                    tenant_id=UUID(payload["tenant_id"]),
                    api_key_id=api_key_id,
                    method=payload["method"],
                    path=payload["path"],
                    status_code=payload["status_code"],
                    latency_ms=payload["latency_ms"],
                    response_size_bytes=payload.get("response_size_bytes"),
                )
            )
            await session.commit()
    finally:
        await engine.dispose()


@celery_app.task(  # type: ignore[untyped-decorator]
    name="api_clients.record_request_log",
    autoretry_for=TRANSIENT_DB_ERRORS,
    max_retries=3,
    default_retry_delay=5,
)
def record_request_log_task(**payload: Any) -> None:
    asyncio.run(_record(payload))


async def _purge(retention_days: int) -> int:
    engine = create_engine(get_settings(), pooled=False)
    try:
        session_factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        async with session_factory() as session:
            result = await session.execute(
                delete(ApiRequestLogModel).where(ApiRequestLogModel.created_at < cutoff)
            )
            await session.commit()
            return cast("CursorResult[Any]", result).rowcount or 0
    finally:
        await engine.dispose()


@celery_app.task(name="api_clients.purge_request_logs")  # type: ignore[untyped-decorator]
def purge_request_logs_task() -> dict[str, int]:
    settings = get_settings()
    deleted = asyncio.run(_purge(settings.api_request_log_retention_days))
    logger.info("api_request_logs_purged", deleted=deleted)
    return {"deleted": deleted}
