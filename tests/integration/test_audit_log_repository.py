import asyncio
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.context import update_request_context
from app.core.db import create_engine
from app.core.pagination import OffsetParams
from app.modules.audit_log.domain.entities import AuditLogEntry
from app.modules.audit_log.infrastructure.models import AuditLogModel
from app.modules.audit_log.infrastructure.repository import AuditLogRepository
from app.modules.audit_log.infrastructure.tasks import _record


async def test_record_round_trips_json_fields(db_session: AsyncSession) -> None:
    audit_logs = AuditLogRepository(db_session)
    entry = AuditLogEntry(
        tenant_id=uuid4(),
        actor_id=uuid4(),
        action="user.updated",
        resource_type="user",
        resource_id="abc",
        before={"name": "old"},
        after={"name": "new"},
    )

    saved = await audit_logs.record(entry)

    assert saved.before == {"name": "old"}
    assert saved.after == {"name": "new"}
    assert saved.created_at is not None


async def test_paginate_orders_by_created_at_descending(db_session: AsyncSession) -> None:
    audit_logs = AuditLogRepository(db_session)
    tenant_id = uuid4()
    update_request_context(tenant_id=tenant_id)

    await audit_logs.record(
        AuditLogEntry(
            tenant_id=tenant_id, actor_id=None, action="a", resource_type="x", resource_id=None
        )
    )
    await audit_logs.record(
        AuditLogEntry(
            tenant_id=tenant_id, actor_id=None, action="b", resource_type="x", resource_id=None
        )
    )

    page = await audit_logs.paginate(OffsetParams())
    created_ats = [e.created_at for e in page.items]
    assert created_ats == sorted(created_ats, reverse=True)
    assert page.total == 2


async def test_paginate_filters_by_resource_type(db_session: AsyncSession) -> None:
    audit_logs = AuditLogRepository(db_session)
    tenant_id = uuid4()
    update_request_context(tenant_id=tenant_id)
    await audit_logs.record(
        AuditLogEntry(
            tenant_id=tenant_id, actor_id=None, action="a", resource_type="user", resource_id=None
        )
    )
    await audit_logs.record(
        AuditLogEntry(
            tenant_id=tenant_id, actor_id=None, action="b", resource_type="role", resource_id=None
        )
    )

    page = await audit_logs.paginate(OffsetParams(), resource_type="role")
    assert all(e.resource_type == "role" for e in page.items)
    assert page.total == 1


async def test_paginate_filters_by_actor_id(db_session: AsyncSession) -> None:
    audit_logs = AuditLogRepository(db_session)
    tenant_id = uuid4()
    actor_id = uuid4()
    update_request_context(tenant_id=tenant_id)
    await audit_logs.record(
        AuditLogEntry(
            tenant_id=tenant_id,
            actor_id=actor_id,
            action="a",
            resource_type="user",
            resource_id=None,
        )
    )
    await audit_logs.record(
        AuditLogEntry(
            tenant_id=tenant_id,
            actor_id=uuid4(),
            action="b",
            resource_type="user",
            resource_id=None,
        )
    )

    page = await audit_logs.paginate(OffsetParams(), actor_id=actor_id)
    assert page.total == 1
    assert page.items[0].actor_id == actor_id


async def test_paginate_is_tenant_scoped(db_session: AsyncSession) -> None:
    audit_logs = AuditLogRepository(db_session)
    tenant_a, tenant_b = uuid4(), uuid4()

    update_request_context(tenant_id=tenant_a)
    await audit_logs.record(
        AuditLogEntry(
            tenant_id=tenant_a, actor_id=None, action="a", resource_type="user", resource_id=None
        )
    )
    update_request_context(tenant_id=tenant_b)
    page = await audit_logs.paginate(OffsetParams())
    assert page.total == 0


async def test_record_audit_event_task_internal_writes_a_readable_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, actor_id = uuid4(), uuid4()
    payload = {
        "tenant_id": tenant_id,
        "actor_id": actor_id,
        "action": "tenant.created",
        "resource_type": "tenant",
        "resource_id": str(tenant_id),
        "before": None,
        "after": None,
        "request_id": "req-x",
        "ip_address": None,
    }

    await _record(payload)

    async with session_factory() as session:
        update_request_context(tenant_id=tenant_id)
        page = await AuditLogRepository(session).paginate(OffsetParams())
        assert any(e.action == "tenant.created" for e in page.items)


async def _count_audit_rows(tenant_id: UUID) -> int:
    # Its own engine: this runs under `asyncio.run()`, a different event loop from
    # the one the shared engine's pool (and the fixtures) are bound to.
    engine = create_engine(get_settings(), pooled=False)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                select(func.count())
                .select_from(AuditLogModel)
                .where(AuditLogModel.tenant_id == tenant_id)
            )
            return result.scalar_one()
    finally:
        await engine.dispose()


def test_record_can_run_repeatedly_via_asyncio_run_like_the_celery_task() -> None:
    """A worker process runs many tasks, each under its own `asyncio.run()` and so
    its own event loop. On Postgres a pool shared across loops fails on the second
    call ("attached to a different loop"); SQLite never shows it."""
    tenant_id = uuid4()
    payload = {
        "tenant_id": tenant_id,
        "actor_id": None,
        "action": "tenant.created",
        "resource_type": "tenant",
        "resource_id": None,
        "before": None,
        "after": None,
        "request_id": None,
        "ip_address": None,
    }

    asyncio.run(_record(payload))
    asyncio.run(_record(payload))

    assert asyncio.run(_count_audit_rows(tenant_id)) == 2
