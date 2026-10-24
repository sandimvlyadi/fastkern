"""GET /roles/permissions and GET /api-clients/scopes — the catalogs a client picks from when
editing a role's permissions or creating an API key."""

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.api_clients.domain.scopes import API_KEY_SCOPE_CATALOG
from app.modules.roles.domain.baseline import TENANT_ROLE_PERMISSIONS
from app.modules.roles.domain.permissions import PERMISSION_CATALOG
from tests.helpers import auth_header, grant_role, register_and_login


async def _token_with(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    email: str,
    permissions: list[str],
) -> str:
    user_id, token, _ = await register_and_login(client, tenant_id, email)
    await grant_role(
        session_factory,
        tenant_id=tenant_id,
        user_id=user_id,
        role_name=f"role-{email.partition('@')[0]}",
        permissions=permissions,
    )
    return token


async def test_list_permissions_requires_roles_read(client: AsyncClient, tenant_id: str) -> None:
    _, token, _ = await register_and_login(client, tenant_id, "noperm@example.com")
    response = await client.get("/api/v1/roles/permissions", headers=auth_header(token))
    assert response.status_code == 403


async def test_wildcard_caller_sees_the_whole_catalog_all_grantable(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    token = await _token_with(client, tenant_id, session_factory, "root@example.com", ["*"])

    response = await client.get(
        "/api/v1/roles/permissions", params={"page_size": 200}, headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert [p["code"] for p in body["data"]] == [p.code for p in PERMISSION_CATALOG]
    assert all(p["grantable"] for p in body["data"])
    wildcard = next(p for p in body["data"] if p["code"] == "*")
    assert wildcard == {
        "code": "*",
        "resource": "*",
        "action": "*",
        "platformOnly": True,
        "grantable": True,
    }
    assert body["meta"]["pagination"]["total"] == len(PERMISSION_CATALOG)


async def test_tenant_admin_never_sees_platform_only_permissions(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    token = await _token_with(
        client,
        tenant_id,
        session_factory,
        "admin@example.com",
        list(TENANT_ROLE_PERMISSIONS["admin"]),
    )

    response = await client.get(
        "/api/v1/roles/permissions", params={"page_size": 200}, headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    codes = {p["code"] for p in response.json()["data"]}
    assert codes == set(TENANT_ROLE_PERMISSIONS["admin"])
    assert all(p["grantable"] and not p["platformOnly"] for p in response.json()["data"])


async def test_partial_caller_gets_grantable_only_for_what_they_hold(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    token = await _token_with(
        client, tenant_id, session_factory, "editor@example.com", ["roles:read", "users:read"]
    )

    response = await client.get(
        "/api/v1/roles/permissions", params={"page_size": 200}, headers=auth_header(token)
    )

    grantable = {p["code"] for p in response.json()["data"] if p["grantable"]}
    assert grantable == {"roles:read", "users:read"}
    assert "users:write" in {p["code"] for p in response.json()["data"]}


async def test_list_permissions_is_paginated(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    token = await _token_with(client, tenant_id, session_factory, "pager@example.com", ["*"])

    response = await client.get(
        "/api/v1/roles/permissions",
        params={"page": 2, "page_size": 5},
        headers=auth_header(token),
    )

    assert [p["code"] for p in response.json()["data"]] == [
        p.code for p in PERMISSION_CATALOG[5:10]
    ]
    assert response.json()["meta"]["pagination"]["page"] == 2


async def test_list_api_key_scopes_requires_api_clients_read(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    no_perm = await _token_with(
        client, tenant_id, session_factory, "scopeless@example.com", ["roles:read"]
    )
    forbidden = await client.get("/api/v1/api-clients/scopes", headers=auth_header(no_perm))
    assert forbidden.status_code == 403

    token = await _token_with(
        client, tenant_id, session_factory, "keys@example.com", ["api_clients:read"]
    )
    response = await client.get("/api/v1/api-clients/scopes", headers=auth_header(token))

    assert response.status_code == 200, response.text
    # The caller holds none of the catalog's permissions, so nothing is grantable by them.
    assert response.json()["data"] == [
        {"code": s.code, "resource": s.resource, "action": s.action, "grantable": False}
        for s in API_KEY_SCOPE_CATALOG
    ]


async def test_create_api_key_with_unknown_scope_is_422(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    token = await _token_with(
        client,
        tenant_id,
        session_factory,
        "keys-writer@example.com",
        ["api_clients:read", "api_clients:write"],
    )

    unknown = await client.post(
        "/api/v1/api-clients",
        json={"name": "Acme", "scopes": ["posts:read", "orders:read"]},
        headers=auth_header(token),
    )
    wildcard = await client.post(
        "/api/v1/api-clients", json={"name": "Acme", "scopes": ["*"]}, headers=auth_header(token)
    )
    listing = await client.get("/api/v1/api-clients", headers=auth_header(token))

    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "unknown_scope"
    assert "orders:read" in unknown.json()["error"]["message"]
    assert wildcard.status_code == 422
    assert wildcard.json()["error"]["code"] == "validation_error"
    assert listing.json()["data"] == []
