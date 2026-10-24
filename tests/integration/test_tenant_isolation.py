"""Regression coverage for the cross-tenant isolation bugs found in code
review: login/register scoping by tenant_id, role assignment rejecting a
target user from a different tenant, and audit-log entries never leaking
across tenants.
"""

from uuid import UUID

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.roles.infrastructure.models import RoleModel, UserRoleModel
from tests.helpers import create_tenant


async def _register_and_login(client: AsyncClient, tenant_id: str, email: str) -> tuple[str, str]:
    """Returns (user_id, access_token)."""
    await client.post(
        "/api/v1/auth/register",
        json={"tenantId": tenant_id, "email": email, "password": "S3curePass!", "fullName": "T"},
    )
    login = await client.post(
        "/api/v1/auth/login",
        json={"tenantId": tenant_id, "email": email, "password": "S3curePass!"},
    )
    assert login.status_code == 200, login.text
    token = login.json()["data"]["accessToken"]
    me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    return me.json()["data"]["id"], token


async def _grant_role(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: str,
    user_id: str,
    role_name: str,
    permissions: list[str],
) -> None:
    async with session_factory() as session:
        role = RoleModel(tenant_id=UUID(tenant_id), name=role_name, permissions=permissions)
        session.add(role)
        await session.flush()
        session.add(
            UserRoleModel(tenant_id=UUID(tenant_id), user_id=UUID(user_id), role_id=role.id)
        )
        await session.commit()


async def test_login_does_not_match_same_email_in_another_tenant(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_a, tenant_b = (
        await create_tenant(session_factory),
        await create_tenant(session_factory),
    )
    email = "shared-email@example.com"

    await client.post(
        "/api/v1/auth/register",
        json={"tenantId": tenant_a, "email": email, "password": "S3curePass!", "fullName": "A"},
    )
    # Same email is allowed to register again under a DIFFERENT tenant
    # (users.tenant_id+email is the real uniqueness constraint).
    register_b = await client.post(
        "/api/v1/auth/register",
        json={"tenantId": tenant_b, "email": email, "password": "OtherPass!2", "fullName": "B"},
    )
    assert register_b.status_code == 201, register_b.text

    # Logging into tenant B with tenant A's password must fail — the
    # lookup has to be scoped by the tenant_id in the request, not global.
    cross_login = await client.post(
        "/api/v1/auth/login", json={"tenantId": tenant_b, "email": email, "password": "S3curePass!"}
    )
    assert cross_login.status_code == 401

    correct_login = await client.post(
        "/api/v1/auth/login", json={"tenantId": tenant_b, "email": email, "password": "OtherPass!2"}
    )
    assert correct_login.status_code == 200


async def test_assign_role_rejects_user_from_another_tenant(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_a, tenant_b = (
        await create_tenant(session_factory),
        await create_tenant(session_factory),
    )

    admin_id, admin_token = await _register_and_login(client, tenant_a, "admin@example.com")
    await _grant_role(
        session_factory, tenant_id=tenant_a, user_id=admin_id, role_name="admin", permissions=["*"]
    )
    other_tenant_user_id, _ = await _register_and_login(client, tenant_b, "victim@example.com")

    async with session_factory() as session:
        role = RoleModel(tenant_id=UUID(tenant_a), name="viewer", permissions=[])
        session.add(role)
        await session.flush()
        role_id = str(role.id)
        await session.commit()

    response = await client.post(
        f"/api/v1/roles/users/{other_tenant_user_id}",
        json={"roleId": role_id},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "user_not_found"


async def test_audit_logs_are_scoped_to_caller_tenant(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_a, tenant_b = (
        await create_tenant(session_factory),
        await create_tenant(session_factory),
    )

    admin_a_id, admin_a_token = await _register_and_login(
        client, tenant_a, "audit-admin-a@example.com"
    )
    await _grant_role(
        session_factory,
        tenant_id=tenant_a,
        user_id=admin_a_id,
        role_name="admin",
        permissions=["*"],
    )
    admin_b_id, admin_b_token = await _register_and_login(
        client, tenant_b, "audit-admin-b@example.com"
    )
    await _grant_role(
        session_factory,
        tenant_id=tenant_b,
        user_id=admin_b_id,
        role_name="admin",
        permissions=["*"],
    )

    # Generate one audit event in each tenant (a self-update).
    for token, user_id in ((admin_a_token, admin_a_id), (admin_b_token, admin_b_id)):
        resp = await client.patch(
            f"/api/v1/users/{user_id}",
            json={"fullName": "Updated Name"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text

    logs_a = await client.get(
        "/api/v1/audit-logs", headers={"Authorization": f"Bearer {admin_a_token}"}
    )
    assert logs_a.status_code == 200
    actor_ids_a = {entry["actorId"] for entry in logs_a.json()["data"]}
    assert admin_a_id in actor_ids_a
    assert admin_b_id not in actor_ids_a
