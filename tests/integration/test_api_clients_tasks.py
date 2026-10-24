"""Celery entry points of the `api_clients` module, called directly.

Plain (non-async) tests on purpose: the tasks run their own `asyncio.run()`, which cannot nest
inside the pytest-asyncio loop — mirrors `tests/integration/test_files_tasks.py`. Setup/assertion
helpers below use their own short-lived engine (via `create_engine`, not the shared `app.core.db`
one) for the same reason, run sequentially through separate `asyncio.run()` calls rather than
sharing a loop with the task under test.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import get_settings
from app.core.db import create_engine
from app.modules.api_clients.infrastructure import tasks
from app.modules.api_clients.infrastructure.models import ApiKeyModel, ApiRequestLogModel
from app.modules.tenants.infrastructure.models import TenantModel


async def _seed_tenant_and_key() -> tuple[UUID, UUID]:
    engine = create_engine(get_settings(), pooled=False)
    try:
        session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with session_factory() as session:
            tenant = TenantModel(name="Task Test Tenant", slug=f"task-test-{uuid4().hex[:12]}")
            session.add(tenant)
            await session.flush()
            api_key = ApiKeyModel(
                tenant_id=tenant.id,
                name="Task Test Key",
                key_prefix="taskkey1",
                key_hash=uuid4().hex,
            )
            session.add(api_key)
            await session.commit()
            return tenant.id, api_key.id
    finally:
        await engine.dispose()


async def _insert_log_row(tenant_id: UUID, api_key_id: UUID, *, created_at: datetime) -> UUID:
    engine = create_engine(get_settings(), pooled=False)
    try:
        session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with session_factory() as session:
            row = ApiRequestLogModel(
                tenant_id=tenant_id,
                api_key_id=api_key_id,
                method="GET",
                path="/partners/posts",
                status_code=200,
                latency_ms=12,
                created_at=created_at,
            )
            session.add(row)
            await session.commit()
            return row.id
    finally:
        await engine.dispose()


async def _count_logs_for(api_key_id: UUID) -> int:
    engine = create_engine(get_settings(), pooled=False)
    try:
        session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with session_factory() as session:
            result = await session.execute(
                select(ApiRequestLogModel).where(ApiRequestLogModel.api_key_id == api_key_id)
            )
            return len(result.scalars().all())
    finally:
        await engine.dispose()


async def _last_used_at(api_key_id: UUID) -> datetime | None:
    engine = create_engine(get_settings(), pooled=False)
    try:
        session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with session_factory() as session:
            key = await session.get(ApiKeyModel, api_key_id)
            assert key is not None
            value = key.last_used_at
            # SQLite drops tzinfo on round-trip; Postgres does not.
            return value.replace(tzinfo=UTC) if value and value.tzinfo is None else value
    finally:
        await engine.dispose()


def _record(api_key_id: UUID, tenant_id: UUID, *, used_at: datetime) -> None:
    tasks.record_request_log_task(
        tenant_id=str(tenant_id),
        api_key_id=str(api_key_id),
        method="GET",
        path="/partners/posts",
        status_code=200,
        latency_ms=7,
        response_size_bytes=None,
        used_at=used_at.isoformat(),
    )


async def _row_exists(row_id: UUID) -> bool:
    engine = create_engine(get_settings(), pooled=False)
    try:
        session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with session_factory() as session:
            result = await session.execute(
                select(ApiRequestLogModel).where(ApiRequestLogModel.id == row_id)
            )
            return result.scalar_one_or_none() is not None
    finally:
        await engine.dispose()


def test_record_request_log_task_writes_a_row() -> None:
    tenant_id, api_key_id = asyncio.run(_seed_tenant_and_key())

    tasks.record_request_log_task(
        tenant_id=str(tenant_id),
        api_key_id=str(api_key_id),
        method="GET",
        path="/partners/posts",
        status_code=200,
        latency_ms=7,
        response_size_bytes=256,
    )

    assert asyncio.run(_count_logs_for(api_key_id)) == 1


def test_record_request_log_task_moves_last_used_at_forward_at_most_once_a_minute() -> None:
    """`last_used_at` is written here, off the request path, and throttled so a busy key costs
    one UPDATE a minute rather than one per request."""
    tenant_id, api_key_id = asyncio.run(_seed_tenant_and_key())
    first = datetime.now(UTC).replace(microsecond=0)

    _record(api_key_id, tenant_id, used_at=first)
    assert asyncio.run(_last_used_at(api_key_id)) == first

    _record(api_key_id, tenant_id, used_at=first + timedelta(seconds=30))
    assert asyncio.run(_last_used_at(api_key_id)) == first  # too soon: left alone

    later = first + timedelta(minutes=2)
    _record(api_key_id, tenant_id, used_at=later)
    assert asyncio.run(_last_used_at(api_key_id)) == later
    assert asyncio.run(_count_logs_for(api_key_id)) == 3  # every request is still logged


def test_record_request_log_task_accepts_a_message_without_used_at() -> None:
    """A message queued by the previous release carries no `used_at`."""
    tenant_id, api_key_id = asyncio.run(_seed_tenant_and_key())

    tasks.record_request_log_task(
        tenant_id=str(tenant_id),
        api_key_id=str(api_key_id),
        method="GET",
        path="/partners/posts",
        status_code=200,
        latency_ms=7,
    )

    assert asyncio.run(_last_used_at(api_key_id)) is not None


def test_purge_request_logs_task_deletes_only_old_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """`purge_request_logs_task` deletes across the whole table (it's a global retention sweep,
    not tenant/key-scoped), so — like `test_files_pipeline.py`'s cleanup tests — this asserts on
    this test's own rows by id rather than on the task's global `deleted` count, which the rest
    of the suite's state can also affect."""
    tenant_id, api_key_id = asyncio.run(_seed_tenant_and_key())
    now = datetime.now(UTC)
    old_row_id = asyncio.run(
        _insert_log_row(tenant_id, api_key_id, created_at=now - timedelta(days=90))
    )
    recent_row_id = asyncio.run(_insert_log_row(tenant_id, api_key_id, created_at=now))

    settings = get_settings().model_copy(update={"api_request_log_retention_days": 60})
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)

    result = tasks.purge_request_logs_task()

    assert result["deleted"] >= 1
    assert not asyncio.run(_row_exists(old_row_id))
    assert asyncio.run(_row_exists(recent_row_id))


def test_purge_request_logs_task_is_a_no_op_when_nothing_is_old(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id, api_key_id = asyncio.run(_seed_tenant_and_key())
    recent_row_id = asyncio.run(
        _insert_log_row(tenant_id, api_key_id, created_at=datetime.now(UTC))
    )

    settings = get_settings().model_copy(update={"api_request_log_retention_days": 60})
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)

    tasks.purge_request_logs_task()

    survived = asyncio.run(_row_exists(recent_row_id))
    assert survived, "a row younger than the retention window survives"
