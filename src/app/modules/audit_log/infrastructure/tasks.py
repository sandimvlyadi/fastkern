"""Fire-and-forget audit writes via Celery, for a mutation endpoint whose
response should not wait on the audit-log insert.

Not dispatched anywhere yet: mutating endpoints call the synchronous
`app.core.di.record_audit_event(...)`, which stays the pattern for a new endpoint. Use this
task only where the write must leave the request path (e.g. a bulk import):
`await dispatch_task("audit_log.record_event", **asdict(input_dto))`. It wraps the same use
case as the synchronous path, so the two record identical events.
"""

import asyncio
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.celery_app import celery_app
from app.core.config import get_settings
from app.core.db import TRANSIENT_DB_ERRORS, create_engine
from app.modules.audit_log.application.dto import RecordAuditEventInput
from app.modules.audit_log.application.use_cases import RecordAuditEventUseCase
from app.modules.audit_log.infrastructure.repository import AuditLogRepository


async def _record(payload: dict[str, Any]) -> None:
    # A task-local engine, not the module-level one in `app.core.db`: every task
    # invocation runs under its own `asyncio.run()` (a fresh event loop), but an
    # asyncpg connection pool is bound to the loop that opened its connections.
    # With the shared engine, the second task in a worker process would reuse a
    # connection from an already-closed loop ("attached to a different loop").
    # SQLite hides this, Postgres does not. Unpooled (each connection lives and
    # dies with this task) and disposed before the loop goes away.
    engine = create_engine(get_settings(), pooled=False)
    try:
        session_factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
        async with session_factory() as session:
            use_case = RecordAuditEventUseCase(AuditLogRepository(session))
            await use_case.execute(RecordAuditEventInput(**payload))
            await session.commit()
    finally:
        await engine.dispose()


@celery_app.task(  # type: ignore[untyped-decorator]
    name="audit_log.record_event",
    autoretry_for=TRANSIENT_DB_ERRORS,
    max_retries=3,
    default_retry_delay=5,
)
def record_audit_event_task(**payload: Any) -> None:
    asyncio.run(_record(payload))
