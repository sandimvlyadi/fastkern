"""POST/PATCH /roles refuse malformed permission strings and ones outside the catalog."""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.helpers import auth_header, create_role, grant_role, register_and_login


async def _superadmin(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> dict[str, str]:
    user_id, token, _ = await register_and_login(client, tenant_id, "b7-super@example.com")
    await grant_role(
        session_factory,
        tenant_id=tenant_id,
        user_id=user_id,
        role_name="superadmin",
        permissions=["*"],
    )
    return auth_header(token)


@pytest.mark.parametrize(
    "permissions", [["not a perm!!", "::"], ["Users:Read"], ["users:read:x"], ["roles:*"]]
)
async def test_create_role_with_malformed_permissions_is_422(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    permissions: list[str],
) -> None:
    headers = await _superadmin(client, tenant_id, session_factory)

    response = await client.post(
        "/api/v1/roles", json={"name": "bad-perms", "permissions": permissions}, headers=headers
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    listing = await client.get("/api/v1/roles", headers=headers)
    assert "bad-perms" not in {r["name"] for r in listing.json()["data"]}


async def test_create_role_with_valid_permissions_is_201_and_deduplicated(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    headers = await _superadmin(client, tenant_id, session_factory)

    response = await client.post(
        "/api/v1/roles",
        json={"name": "good-perms", "permissions": ["users:read", "users:read", "roles:read"]},
        headers=headers,
    )

    assert response.status_code == 201, response.text
    assert response.json()["data"]["permissions"] == ["users:read", "roles:read"]


async def test_update_role_with_malformed_permissions_is_422_and_changes_nothing(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    headers = await _superadmin(client, tenant_id, session_factory)
    created = await client.post(
        "/api/v1/roles", json={"name": "editable", "permissions": ["users:read"]}, headers=headers
    )
    role_id = created.json()["data"]["id"]

    response = await client.patch(
        f"/api/v1/roles/{role_id}", json={"permissions": ["!!"]}, headers=headers
    )

    assert response.status_code == 422
    listing = await client.get("/api/v1/roles", headers=headers)
    role = next(r for r in listing.json()["data"] if r["id"] == role_id)
    assert role["permissions"] == ["users:read"]


async def test_create_role_with_unknown_permission_is_422_even_for_a_wildcard_caller(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    headers = await _superadmin(client, tenant_id, session_factory)

    response = await client.post(
        "/api/v1/roles",
        json={"name": "typo-perms", "permissions": ["users:read", "user:read"]},
        headers=headers,
    )

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "unknown_permission"
    assert "user:read" in error["message"]
    listing = await client.get("/api/v1/roles", headers=headers)
    assert "typo-perms" not in {r["name"] for r in listing.json()["data"]}


async def test_update_role_rejects_an_added_unknown_permission_but_keeps_a_stale_one(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    headers = await _superadmin(client, tenant_id, session_factory)
    # Seeded directly: a permission stored before the catalog existed (or since retired).
    stale = await create_role(
        session_factory,
        tenant_id=tenant_id,
        role_name="legacy",
        permissions=["users:read", "retired:read"],
    )

    rejected = await client.patch(
        f"/api/v1/roles/{stale}",
        json={"permissions": ["users:read", "retired:read", "nope:read"]},
        headers=headers,
    )
    kept = await client.patch(
        f"/api/v1/roles/{stale}",
        json={"permissions": ["retired:read", "users:write"]},
        headers=headers,
    )

    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "unknown_permission"
    assert kept.status_code == 200, kept.text
    assert kept.json()["data"]["permissions"] == ["retired:read", "users:write"]
