from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.helpers import auth_header, grant_role, register_and_login


async def _admin(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    email: str,
) -> tuple[str, str]:
    user_id, token, _ = await register_and_login(client, tenant_id, email)
    await grant_role(
        session_factory, tenant_id=tenant_id, user_id=user_id, role_name="admin", permissions=["*"]
    )
    return user_id, token


async def test_list_audit_logs_requires_permission(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, no_perm_token, _ = await register_and_login(client, tenant_id, "noperm@example.com")
    response = await client.get("/api/v1/audit-logs", headers=auth_header(no_perm_token))
    assert response.status_code == 403


async def test_list_audit_logs_pagination_meta(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    admin_id, admin_token = await _admin(client, tenant_id, session_factory, "admin@example.com")
    # Generate at least one audit event to list.
    await client.patch(
        f"/api/v1/users/{admin_id}", json={"fullName": "Renamed"}, headers=auth_header(admin_token)
    )

    response = await client.get("/api/v1/audit-logs", headers=auth_header(admin_token))
    assert response.status_code == 200
    meta = response.json()["meta"]["pagination"]
    assert meta["page"] == 1
    assert meta["pageSize"] == 20
    assert meta["total"] >= 1


async def test_list_audit_logs_filters_by_resource_type(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    admin_id, admin_token = await _admin(client, tenant_id, session_factory, "admin2@example.com")
    await client.patch(
        f"/api/v1/users/{admin_id}", json={"fullName": "Renamed"}, headers=auth_header(admin_token)
    )
    await client.post(
        "/api/v1/roles",
        json={"name": "filtered-role", "permissions": []},
        headers=auth_header(admin_token),
    )

    response = await client.get(
        "/api/v1/audit-logs", params={"resource_type": "role"}, headers=auth_header(admin_token)
    )
    assert response.status_code == 200
    entries = response.json()["data"]
    assert entries
    assert all(e["resourceType"] == "role" for e in entries)


async def test_list_audit_logs_invalid_page_returns_422_not_500(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, admin_token = await _admin(client, tenant_id, session_factory, "admin-page@example.com")

    response = await client.get("/api/v1/audit-logs?page=0", headers=auth_header(admin_token))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_list_audit_logs_filters_by_actor_id(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    admin_id, admin_token = await _admin(client, tenant_id, session_factory, "admin3@example.com")
    other_id, other_token, _ = await register_and_login(client, tenant_id, "other@example.com")
    await grant_role(
        session_factory,
        tenant_id=tenant_id,
        user_id=other_id,
        role_name="other-admin",
        permissions=["*"],
    )

    await client.patch(
        f"/api/v1/users/{admin_id}", json={"fullName": "By Admin"}, headers=auth_header(admin_token)
    )
    await client.patch(
        f"/api/v1/users/{other_id}", json={"fullName": "By Other"}, headers=auth_header(other_token)
    )

    response = await client.get(
        "/api/v1/audit-logs", params={"actor_id": admin_id}, headers=auth_header(admin_token)
    )
    assert response.status_code == 200
    entries = response.json()["data"]
    assert entries
    assert all(e["actorId"] == admin_id for e in entries)
