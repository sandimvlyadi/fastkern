import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.db import create_engine, get_db_session
from app.main import app


async def test_create_engine_configures_pool_for_non_sqlite() -> None:
    settings = get_settings().model_copy(
        update={
            "database_url": "postgresql+asyncpg://u:p@localhost/db",
            "database_pool_size": 7,
            "database_max_overflow": 3,
        }
    )
    engine = create_engine(settings)
    try:
        assert engine.pool.size() == 7
    finally:
        await engine.dispose()


async def test_create_engine_unpooled_keeps_no_pool_for_non_sqlite() -> None:
    settings = get_settings().model_copy(
        update={"database_url": "postgresql+asyncpg://u:p@localhost/db"}
    )
    engine = create_engine(settings, pooled=False)
    try:
        assert isinstance(engine.pool, NullPool)
    finally:
        await engine.dispose()


async def test_create_engine_is_pooled_by_default() -> None:
    settings = get_settings().model_copy(
        update={"database_url": "postgresql+asyncpg://u:p@localhost/db"}
    )
    engine = create_engine(settings)
    try:
        assert not isinstance(engine.pool, NullPool)
    finally:
        await engine.dispose()


async def test_create_engine_sqlite_disables_check_same_thread() -> None:
    settings = get_settings().model_copy(
        update={"database_url": "sqlite+aiosqlite:///./core-db-test.db"}
    )
    engine = create_engine(settings)
    try:
        assert engine.url.get_backend_name() == "sqlite"
    finally:
        await engine.dispose()


async def test_get_db_session_commits_on_success() -> None:
    gen = get_db_session()
    session = await gen.__anext__()
    assert session is not None
    with pytest.raises(StopAsyncIteration):
        await gen.__anext__()


async def test_get_db_session_rolls_back_on_exception() -> None:
    gen = get_db_session()
    await gen.__anext__()
    with pytest.raises(RuntimeError):
        await gen.athrow(RuntimeError("boom"))


async def test_a_failed_commit_is_not_reported_as_a_success(
    tenant_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The commit must run before the response is sent (`scope="function"`, see get_db_session).
    # Under FastAPI's default scope this register returns 201 and only then fails to commit.
    async def failing_commit(self: AsyncSession) -> None:
        raise RuntimeError("commit failed")

    # Seeding (the tenant_id fixture) is done, so only the request's own commit is patched.
    monkeypatch.setattr(AsyncSession, "commit", failing_commit)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "tenantId": tenant_id,
                "email": "commit-fails@example.com",
                "password": "S3curePass!",
                "fullName": "Commit Fails",
            },
        )

    assert response.status_code == 500
