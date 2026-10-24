"""Check-then-insert races end in the documented 409 / idempotent success, never a 500.

Postgres only: these need real concurrent transactions and unique-constraint enforcement
between them (the SQLite test database serialises writers, so there is no race to lose).
"""

import asyncio
from collections import Counter
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.context import update_request_context
from app.core.db import get_engine
from app.modules.roles.infrastructure.repository import UserRoleRepository
from tests.helpers import PASSWORD, auth_header, grant_role, register_and_login

pytestmark = pytest.mark.skipif(
    get_engine().dialect.name != "postgresql",
    reason="needs concurrent Postgres transactions; see docs/testing.md to run the suite there",
)

PARALLEL = 8


def _statuses(responses: list[Response]) -> Counter[int]:
    return Counter(r.status_code for r in responses)


async def _caller(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    permissions: list[str],
) -> dict[str, str]:
    email = f"caller-{uuid4().hex[:8]}@example.com"
    user_id, token, _ = await register_and_login(client, tenant_id, email)
    await grant_role(
        session_factory,
        tenant_id=tenant_id,
        user_id=user_id,
        role_name=f"caller-{uuid4().hex[:8]}",
        permissions=permissions,
    )
    return auth_header(token)


async def test_parallel_registrations_of_one_email_yield_one_201_and_the_rest_409(
    client: AsyncClient, tenant_id: str
) -> None:
    payload = {
        "tenantId": tenant_id,
        "email": "race@example.com",
        "password": PASSWORD,
        "fullName": "Racer",
    }

    responses = await asyncio.gather(
        *(client.post("/api/v1/auth/register", json=payload) for _ in range(PARALLEL))
    )

    assert _statuses(responses) == {201: 1, 409: PARALLEL - 1}
    losers = [r for r in responses if r.status_code == 409]
    assert {r.json()["error"]["code"] for r in losers} == {"email_already_registered"}


async def test_parallel_role_creation_with_one_name_yields_one_201_and_the_rest_409(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    headers = await _caller(client, tenant_id, session_factory, ["roles:read", "roles:write"])

    responses = await asyncio.gather(
        *(
            client.post(
                "/api/v1/roles", json={"name": "race-role", "permissions": []}, headers=headers
            )
            for _ in range(PARALLEL)
        )
    )

    assert _statuses(responses) == {201: 1, 409: PARALLEL - 1}
    losers = [r for r in responses if r.status_code == 409]
    assert {r.json()["error"]["code"] for r in losers} == {"role_already_exists"}


async def test_parallel_tenant_creation_with_one_slug_yields_one_201_and_the_rest_409(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    headers = await _caller(client, tenant_id, session_factory, ["*"])
    slug = f"race-{uuid4().hex[:10]}"

    responses = await asyncio.gather(
        *(
            client.post("/api/v1/tenants", json={"name": "Race", "slug": slug}, headers=headers)
            for _ in range(PARALLEL)
        )
    )

    assert _statuses(responses) == {201: 1, 409: PARALLEL - 1}
    losers = [r for r in responses if r.status_code == 409]
    assert {r.json()["error"]["code"] for r in losers} == {"tenant_slug_already_exists"}


async def test_parallel_role_assignments_are_all_204_and_leave_exactly_one_grant(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    headers = await _caller(client, tenant_id, session_factory, ["roles:read", "roles:write"])
    role = await client.post(
        "/api/v1/roles", json={"name": "assignable", "permissions": []}, headers=headers
    )
    role_id = role.json()["data"]["id"]
    target_id, _, _ = await register_and_login(client, tenant_id, "assign-target@example.com")

    responses = await asyncio.gather(
        *(
            client.post(
                f"/api/v1/roles/users/{target_id}", json={"roleId": role_id}, headers=headers
            )
            for _ in range(PARALLEL)
        )
    )

    assert _statuses(responses) == {204: PARALLEL}
    listing = await client.get(f"/api/v1/roles/users/{target_id}", headers=headers)
    assert [r["id"] for r in listing.json()["data"]].count(role_id) == 1


async def test_concurrent_first_role_assignment_of_different_roles_retries_instead_of_500(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A brand-new user has zero roles, so two concurrent assignments of *different* roles can
    both compute `is_default=True` before either commits. `asyncio.gather` over HTTP requests
    doesn't reliably reproduce this interleaving (two fast, mostly-await requests rarely land
    at exactly the same point), so this drives `UserRoleRepository` directly with two
    independent sessions to force it deterministically — the same technique used to find the
    bug in the first place. Without the retry in `UserRoleRepository.assign`, the second
    `assign()` call below raises `IntegrityError` on
    `uq_sys_user_roles_tenant_user_default` instead of returning cleanly."""
    headers = await _caller(client, tenant_id, session_factory, ["roles:read", "roles:write"])
    role_a = await client.post(
        "/api/v1/roles", json={"name": "race-determ-a", "permissions": []}, headers=headers
    )
    role_b = await client.post(
        "/api/v1/roles", json={"name": "race-determ-b", "permissions": []}, headers=headers
    )
    role_a_id = UUID(role_a.json()["data"]["id"])
    role_b_id = UUID(role_b.json()["data"]["id"])
    target_id, _, _ = await register_and_login(client, tenant_id, "race-determ-target@example.com")
    user_id = UUID(target_id)
    tid = UUID(tenant_id)

    async with session_factory() as session_a, session_factory() as session_b:
        update_request_context(tenant_id=tid)
        user_roles_a = UserRoleRepository(session_a)
        user_roles_b = UserRoleRepository(session_b)
        # Both see zero existing roles before either writes: the exact
        # interleaving two genuinely simultaneous requests would hit.
        assert await user_roles_a.list_roles_for_user(user_id) == []
        assert await user_roles_b.list_roles_for_user(user_id) == []
        assert await user_roles_a.assign(user_id, role_a_id, is_default=True) is True
        await session_a.commit()
        # session_b's insert now races against session_a's already-committed
        # default row. This must retry as non-default, not raise.
        assert await user_roles_b.assign(user_id, role_b_id, is_default=True) is True
        await session_b.commit()

    async with session_factory() as session:
        update_request_context(tenant_id=tid)
        user_roles = UserRoleRepository(session)
        all_roles = await user_roles.list_roles_for_user(user_id)
        default_role = await user_roles.get_default_role_for_user(user_id)

    assert {r.id for r in all_roles} == {role_a_id, role_b_id}
    assert default_role is not None
    assert default_role.id == role_a_id, "the first committed assignment keeps the default"
