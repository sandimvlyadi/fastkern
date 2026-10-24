"""`updated_at` is maintained by a Postgres trigger, not only by the ORM's
`onupdate`, so writes that bypass the unit of work still bump it. SQLite has
no such trigger (the ORM `onupdate` covers it there), so this runs on the
`test-postgres` CI job only."""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import get_engine
from app.modules.tenants.infrastructure.models import TenantModel
from app.shared.infrastructure.orm_base import Base
from app.shared.infrastructure.pg_triggers import tables_missing_updated_at_trigger


@pytest.fixture
def _postgres_only(db_session: AsyncSession) -> None:
    if db_session.get_bind().dialect.name != "postgresql":
        pytest.skip("trigger-maintained updated_at is Postgres-only")


@pytest.mark.usefixtures("_postgres_only")
async def test_raw_bulk_update_bumps_updated_at(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = TenantModel(name="Trigger Co", slug=f"trigger-{uuid4().hex[:8]}")
    async with session_factory() as session:
        session.add(tenant)
        await session.commit()
        created_updated_at = (
            await session.execute(
                text("SELECT updated_at FROM sys_tenants WHERE id = :id"), {"id": tenant.id}
            )
        ).scalar_one()

    await asyncio.sleep(0.01)  # now() is per-transaction: the UPDATE must be a later one

    async with session_factory() as session:
        # Raw SQL on purpose: no ORM unit of work, so `onupdate` cannot fire.
        await session.execute(
            text("UPDATE sys_tenants SET name = 'Renamed' WHERE id = :id"), {"id": tenant.id}
        )
        await session.commit()
        after = (
            await session.execute(
                text("SELECT updated_at FROM sys_tenants WHERE id = :id"), {"id": tenant.id}
            )
        ).scalar_one()

    assert after > created_updated_at


@pytest.mark.usefixtures("_postgres_only")
async def test_guard_reports_no_table_missing_its_trigger() -> None:
    async with get_engine().connect() as connection:
        assert await tables_missing_updated_at_trigger(connection) == []


@pytest.mark.usefixtures("_postgres_only")
async def test_guard_names_a_table_whose_trigger_is_missing_or_disabled() -> None:
    """DDL is transactional on Postgres: break the schema, look, roll back."""
    async with get_engine().connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.execute(text("DROP TRIGGER trg_sys_users_set_updated_at ON sys_users"))
            await connection.execute(
                text("ALTER TABLE sys_roles DISABLE TRIGGER trg_sys_roles_set_updated_at")
            )
            missing = await tables_missing_updated_at_trigger(connection)
        finally:
            await transaction.rollback()
    assert missing == ["sys_roles", "sys_users"]


_TRIGGER_TABLES = text(
    """
    SELECT c.relname
    FROM pg_trigger g
    JOIN pg_class c ON c.oid = g.tgrelid
    JOIN pg_proc p ON p.oid = g.tgfoid AND p.proname = 'sys_set_updated_at'
    WHERE NOT g.tgisinternal
    ORDER BY c.relname
    """
)
_FUNCTION_EXISTS = text("SELECT count(*) FROM pg_proc WHERE proname = 'sys_set_updated_at'")


@pytest.mark.usefixtures("_postgres_only")
async def test_partial_drop_all_keeps_the_function_while_other_tables_use_it() -> None:
    """Dropping some tables must not try to drop the shared function (Postgres
    refuses while other tables' triggers depend on it); dropping all of them
    must remove it. DDL is transactional on Postgres, so this rolls back."""
    async with get_engine().connect() as connection:
        transaction = await connection.begin()
        try:
            user_roles = Base.metadata.tables["sys_user_roles"]
            await connection.run_sync(lambda c: Base.metadata.drop_all(c, tables=[user_roles]))
            assert (await connection.execute(_FUNCTION_EXISTS)).scalar_one() == 1

            await connection.run_sync(Base.metadata.drop_all)
            assert (await connection.execute(_FUNCTION_EXISTS)).scalar_one() == 0
        finally:
            await transaction.rollback()


@pytest.mark.usefixtures("_postgres_only")
async def test_partial_create_all_adds_triggers_only_to_the_tables_it_creates() -> None:
    async with get_engine().connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.run_sync(Base.metadata.drop_all)
            tenants = Base.metadata.tables["sys_tenants"]
            await connection.run_sync(lambda c: Base.metadata.create_all(c, tables=[tenants]))
            created = [row[0] for row in await connection.execute(_TRIGGER_TABLES)]
            assert created == ["sys_tenants"]
        finally:
            await transaction.rollback()
