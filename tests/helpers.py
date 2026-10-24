"""Shared helpers for integration tests: HTTP flows and DB seeding that
several test modules would otherwise duplicate."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID, uuid4

from httpx import AsyncClient
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.security import hash_password
from app.modules.roles.infrastructure.models import RoleModel, UserRoleModel
from app.modules.tenants.infrastructure.models import TenantModel

# Fixtures (see conftest.py) that persist a tenant / a user of that tenant in the
# test's own session and return its id.
TenantFactory = Callable[[], Awaitable[UUID]]
UserFactory = Callable[[UUID], Awaitable[UUID]]
# Returns (api_key_id, plaintext) — the plaintext is only ever available at creation time.
ApiKeyFactory = Callable[..., Awaitable[tuple[UUID, str]]]

# A database name with a whole-word "test"/"tests" part (`app_test`, `test-db`, `tests`).
# Deliberately not a substring match: `latest`, `contest` and `attestation` contain "test".
_TEST_DATABASE_NAME = re.compile(r"(?:^|[_-])tests?(?:$|[_-])", re.IGNORECASE)


def is_disposable_test_database(url: URL) -> bool:
    """Whether the schema fixture may `drop_all` on `url` when the session ends:
    SQLite always (the default), anything else only if it is *named* like a test database."""
    if url.get_backend_name() == "sqlite":
        return True
    return _TEST_DATABASE_NAME.search(url.database or "") is not None


PASSWORD = "S3curePass!"
HASHED_PASSWORD = hash_password(PASSWORD)


async def create_tenant(session_factory: async_sessionmaker[AsyncSession]) -> str:
    """Insert an active tenant row and return its id (registration rejects unknown tenants)."""
    tenant = TenantModel(name="Test Tenant", slug=f"test-{uuid4().hex[:12]}")
    async with session_factory() as session:
        session.add(tenant)
        await session.commit()
    return str(tenant.id)


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def register_and_login(
    client: AsyncClient, tenant_id: str, email: str, *, full_name: str = "Test User"
) -> tuple[str, str, str]:
    """Registers + logs in a user, returning (user_id, access_token, refresh_token)."""
    await client.post(
        "/api/v1/auth/register",
        json={
            "tenantId": tenant_id,
            "email": email,
            "password": PASSWORD,
            "fullName": full_name,
        },
    )
    login = await client.post(
        "/api/v1/auth/login",
        json={"tenantId": tenant_id, "email": email, "password": PASSWORD},
    )
    assert login.status_code == 200, login.text
    data = login.json()["data"]
    access_token, refresh_token = data["accessToken"], data["refreshToken"]

    me = await client.get("/api/v1/auth/me", headers=auth_header(access_token))
    user_id = me.json()["data"]["id"]
    return user_id, access_token, refresh_token


async def grant_role(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: str,
    user_id: str,
    role_name: str,
    permissions: list[str],
) -> str:
    """Creates a role and assigns it to `user_id`; returns the role id."""
    async with session_factory() as session:
        role = RoleModel(tenant_id=UUID(tenant_id), name=role_name, permissions=permissions)
        session.add(role)
        await session.flush()
        role_id = str(role.id)
        session.add(
            UserRoleModel(tenant_id=UUID(tenant_id), user_id=UUID(user_id), role_id=role.id)
        )
        await session.commit()
    return role_id


async def create_role(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: str,
    role_name: str,
    permissions: list[str],
) -> str:
    """Creates a role in a tenant without assigning it to anyone; returns the role id."""
    async with session_factory() as session:
        role = RoleModel(tenant_id=UUID(tenant_id), name=role_name, permissions=permissions)
        session.add(role)
        await session.commit()
    return str(role.id)


async def audit_entries(client: AsyncClient, token: str, action: str) -> list[dict[str, Any]]:
    """Audit-log entries with the given `action`, read through the public API
    (needs a token whose user holds `audit_log:read`)."""
    response = await client.get("/api/v1/audit-logs", headers=auth_header(token))
    assert response.status_code == 200, response.text
    return [e for e in response.json()["data"] if e["action"] == action]
