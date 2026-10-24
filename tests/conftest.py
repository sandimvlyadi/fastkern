from collections.abc import AsyncGenerator, Generator
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.core.rate_limit as rate_limit_module
from app.core.context import reset_request_context
from app.core.db import get_engine, get_session_factory
from app.main import app
from app.modules.api_clients.domain.hashing import generate_api_key

# Import every module's ORM models so their tables register on Base.metadata
# before create_all runs.
from app.modules.api_clients.infrastructure import models as _api_clients_models  # noqa: F401
from app.modules.api_clients.infrastructure.models import ApiKeyModel
from app.modules.audit_log.infrastructure import models as _audit_log_models  # noqa: F401
from app.modules.examples.infrastructure import models as _examples_models  # noqa: F401
from app.modules.master_examples.infrastructure import (
    models as _master_examples_models,  # noqa: F401
)
from app.modules.roles.infrastructure import models as _roles_models  # noqa: F401
from app.modules.tenants.infrastructure import models as _tenants_models  # noqa: F401
from app.modules.tenants.infrastructure.models import TenantModel
from app.modules.users.infrastructure import models as _users_models  # noqa: F401
from app.modules.users.infrastructure.models import UserModel
from app.shared.infrastructure.orm_base import Base
from tests.helpers import (
    HASHED_PASSWORD,
    ApiKeyFactory,
    TenantFactory,
    UserFactory,
    is_disposable_test_database,
)


def _assert_disposable_database() -> None:
    """The schema fixture below runs `drop_all` at the end of the session, so it
    must never be pointed at a database anyone cares about."""
    url = get_engine().url
    if not is_disposable_test_database(url):
        raise RuntimeError(
            f"Refusing to run the test suite against database {url.database!r}: the schema "
            "fixture drops every table when the session ends. Point DATABASE_URL at a "
            "throwaway database with a 'test' part in its name, e.g. 'app_test'."
        )


@pytest.fixture(scope="session", autouse=True)
async def _create_schema() -> AsyncGenerator[None]:
    _assert_disposable_database()
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
def session_factory() -> async_sessionmaker[AsyncSession]:
    return get_session_factory()


@pytest.fixture
async def db_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession]:
    async with session_factory() as session:
        yield session
        await session.rollback()


@pytest.fixture
def create_db_tenant(db_session: AsyncSession) -> TenantFactory:
    """Persists a tenant in the test's own session (rolled back afterwards).

    Repository tests that insert tenant-owned rows need a real tenant: `tenant_id`
    is a foreign key, which Postgres enforces (SQLite does not), so a made-up
    `uuid4()` only works on SQLite."""

    async def _create() -> UUID:
        tenant = TenantModel(name="Test Tenant", slug=f"test-{uuid4().hex[:12]}")
        db_session.add(tenant)
        await db_session.flush()
        return tenant.id

    return _create


@pytest.fixture
def create_db_user(db_session: AsyncSession) -> UserFactory:
    """Persists a user of `tenant_id` in the test's own session (rolled back
    afterwards) — for rows that reference `users.id`, such as `user_roles`."""

    async def _create(tenant_id: UUID) -> UUID:
        user = UserModel(
            tenant_id=tenant_id,
            email=f"{uuid4().hex}@example.com",
            hashed_password=HASHED_PASSWORD,
            full_name="Test User",
        )
        db_session.add(user)
        await db_session.flush()
        return user.id

    return _create


@pytest.fixture
def create_db_api_key(db_session: AsyncSession) -> ApiKeyFactory:
    """Persists an active API key of `tenant_id` (rolled back afterwards) and
    returns `(api_key_id, plaintext)` — the plaintext is only ever available
    at creation time, same as the real create-key endpoint."""

    async def _create(tenant_id: UUID, *, scopes: list[str] | None = None) -> tuple[UUID, str]:
        plaintext, key_prefix, key_hash = generate_api_key()
        api_key = ApiKeyModel(
            tenant_id=tenant_id,
            name="Test Partner Key",
            key_prefix=key_prefix,
            key_hash=key_hash,
            scopes=scopes if scopes is not None else ["posts:read"],
        )
        db_session.add(api_key)
        await db_session.flush()
        return api_key.id, plaintext

    return _create


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def tenant_id(session_factory: async_sessionmaker[AsyncSession]) -> str:
    """A real, active tenant row — registration rejects unknown tenant ids."""
    tenant = TenantModel(name="Test Tenant", slug=f"test-{uuid4().hex[:12]}")
    async with session_factory() as session:
        session.add(tenant)
        await session.commit()
    return str(tenant.id)


@pytest.fixture(autouse=True)
def _reset_rate_limit_health() -> Generator[None]:
    """`rate_limit_backend_degraded` is process-global (it looks at every live
    `RateLimiter`), so a test that breaks a limiter — on purpose — must not leave
    it counting for whichever test runs next (e.g. the /metrics assertions)."""
    rate_limit_module._LIMITERS.clear()
    yield
    rate_limit_module._LIMITERS.clear()


@pytest.fixture(autouse=True)
def _reset_request_context() -> Generator[None]:
    """A test that calls `update_request_context`/`set_request_context`
    directly (no HTTP request in between) would otherwise leak that
    context into whichever test runs next, since it's a ContextVar on
    the shared test-runner thread."""
    reset_request_context()
    yield
    reset_request_context()
