"""User lifecycle guards over HTTP: no self-lockout and reactivation."""

from typing import Any
from uuid import uuid4

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.helpers import (
    audit_entries,
    auth_header,
    create_tenant,
    grant_role,
    register_and_login,
)


async def _admin(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> tuple[str, str]:
    """(user_id, access_token) of a user holding the users:* and audit permissions."""
    user_id, token, _ = await register_and_login(client, tenant_id, f"a-{uuid4().hex[:8]}@x.io")
    await grant_role(
        session_factory,
        tenant_id=tenant_id,
        user_id=user_id,
        role_name="admin",
        permissions=["users:read", "users:write", "users:delete", "audit_log:read"],
    )
    return user_id, token


async def _audit(client: AsyncClient, token: str, action: str) -> list[dict[str, Any]]:
    return await audit_entries(client, token, action)


# --- No self-lockout ------------------------------------------------------------------------------


async def test_admin_cannot_deactivate_themselves(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    admin_id, token = await _admin(client, tenant_id, session_factory)

    response = await client.post(f"/api/v1/users/{admin_id}/deactivate", headers=auth_header(token))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "cannot_modify_self"
    still_in = await client.get("/api/v1/auth/me", headers=auth_header(token))
    assert still_in.status_code == 200  # not locked out
    assert await _audit(client, token, "user.deactivated") == []


async def test_admin_cannot_delete_themselves(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    admin_id, token = await _admin(client, tenant_id, session_factory)

    response = await client.delete(f"/api/v1/users/{admin_id}", headers=auth_header(token))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "cannot_modify_self"
    assert (await client.get("/api/v1/auth/me", headers=auth_header(token))).status_code == 200


async def test_self_modification_error_is_translated(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    admin_id, token = await _admin(client, tenant_id, session_factory)

    response = await client.post(
        f"/api/v1/users/{admin_id}/deactivate",
        headers={**auth_header(token), "Accept-Language": "id"},
    )

    assert response.json()["error"]["message"].startswith("Anda tidak dapat")


async def test_another_admin_can_still_deactivate_and_delete_a_user(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, token = await _admin(client, tenant_id, session_factory)
    first_id, _, _ = await register_and_login(client, tenant_id, "first@example.com")
    second_id, _, _ = await register_and_login(client, tenant_id, "second@example.com")

    deactivated = await client.post(
        f"/api/v1/users/{first_id}/deactivate", headers=auth_header(token)
    )
    deleted = await client.delete(f"/api/v1/users/{second_id}", headers=auth_header(token))

    assert deactivated.status_code == 200
    assert deleted.status_code == 204


# --- Reactivation ---------------------------------------------------------------------------------


async def test_activate_restores_a_deactivated_user_and_their_old_token(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, token = await _admin(client, tenant_id, session_factory)
    target_id, target_token, _ = await register_and_login(client, tenant_id, "back@example.com")
    await client.post(f"/api/v1/users/{target_id}/deactivate", headers=auth_header(token))
    blocked = await client.get("/api/v1/auth/me", headers=auth_header(target_token))
    assert blocked.status_code == 401

    response = await client.post(f"/api/v1/users/{target_id}/activate", headers=auth_header(token))

    assert response.status_code == 200, response.text
    assert response.json()["data"]["isActive"] is True
    restored = await client.get("/api/v1/auth/me", headers=auth_header(target_token))
    assert restored.status_code == 200


async def test_activate_records_an_audit_event_with_before_and_after(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    admin_id, token = await _admin(client, tenant_id, session_factory)
    target_id, _, _ = await register_and_login(client, tenant_id, "audited@example.com")
    await client.post(f"/api/v1/users/{target_id}/deactivate", headers=auth_header(token))
    await client.post(f"/api/v1/users/{target_id}/activate", headers=auth_header(token))

    (entry,) = await _audit(client, token, "user.activated")

    assert entry["resourceId"] == target_id
    assert entry["actorId"] == admin_id
    assert entry["before"]["isActive"] is False
    assert entry["after"]["isActive"] is True


async def test_activating_an_active_user_is_idempotent(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, token = await _admin(client, tenant_id, session_factory)
    target_id, _, _ = await register_and_login(client, tenant_id, "already@example.com")

    response = await client.post(f"/api/v1/users/{target_id}/activate", headers=auth_header(token))

    assert response.status_code == 200
    assert response.json()["data"]["isActive"] is True


async def test_activate_unknown_user_is_404(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, token = await _admin(client, tenant_id, session_factory)

    response = await client.post(f"/api/v1/users/{uuid4()}/activate", headers=auth_header(token))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "user_not_found"


async def test_activate_requires_users_write(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    viewer_id, viewer_token, _ = await register_and_login(client, tenant_id, "viewer@example.com")
    await grant_role(
        session_factory,
        tenant_id=tenant_id,
        user_id=viewer_id,
        role_name="viewer",
        permissions=["users:read"],
    )

    response = await client.post(
        f"/api/v1/users/{viewer_id}/activate", headers=auth_header(viewer_token)
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "insufficient_permission"


async def test_activate_cannot_reach_a_user_in_another_tenant(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, token = await _admin(client, tenant_id, session_factory)
    other_tenant = await create_tenant(session_factory)
    stranger_id, _, _ = await register_and_login(client, other_tenant, "stranger@example.com")

    response = await client.post(
        f"/api/v1/users/{stranger_id}/activate", headers=auth_header(token)
    )

    assert response.status_code == 404
