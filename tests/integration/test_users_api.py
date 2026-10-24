from uuid import UUID, uuid4

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.helpers import audit_entries, auth_header, grant_role, register_and_login


async def _admin(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    email: str,
) -> tuple[str, str]:
    """Returns (user_id, access_token) for a user with every users:* permission."""
    user_id, token, _ = await register_and_login(client, tenant_id, email)
    await grant_role(
        session_factory,
        tenant_id=tenant_id,
        user_id=user_id,
        role_name="admin",
        permissions=["users:read", "users:write", "users:delete", "audit_log:read"],
    )
    return user_id, token


async def test_get_user_found_and_not_found(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    admin_id, admin_token = await _admin(client, tenant_id, session_factory, "admin@example.com")

    response = await client.get(f"/api/v1/users/{admin_id}", headers=auth_header(admin_token))
    assert response.status_code == 200
    assert response.json()["data"]["email"] == "admin@example.com"

    missing = await client.get(f"/api/v1/users/{uuid4()}", headers=auth_header(admin_token))
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "user_not_found"


async def test_list_users_pagination_meta(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, admin_token = await _admin(client, tenant_id, session_factory, "admin2@example.com")

    response = await client.get("/api/v1/users", headers=auth_header(admin_token))
    assert response.status_code == 200
    meta = response.json()["meta"]["pagination"]
    assert meta["page"] == 1
    assert meta["pageSize"] == 20
    assert meta["total"] >= 1


async def test_update_user_success_records_audit_event(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    admin_id, admin_token = await _admin(client, tenant_id, session_factory, "admin3@example.com")

    response = await client.patch(
        f"/api/v1/users/{admin_id}",
        json={"fullName": "New Name"},
        headers=auth_header(admin_token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["fullName"] == "New Name"

    logs = await client.get("/api/v1/audit-logs", headers=auth_header(admin_token))
    actions = {entry["action"] for entry in logs.json()["data"]}
    assert "user.updated" in actions


async def test_list_users_invalid_page_returns_422_not_500(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, admin_token = await _admin(client, tenant_id, session_factory, "admin-page@example.com")

    response = await client.get("/api/v1/users?page=0", headers=auth_header(admin_token))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_update_user_not_found(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, admin_token = await _admin(client, tenant_id, session_factory, "admin4@example.com")

    response = await client.patch(
        f"/api/v1/users/{uuid4()}", json={"fullName": "X"}, headers=auth_header(admin_token)
    )
    assert response.status_code == 404


async def test_deactivate_user_then_token_is_rejected(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, admin_token = await _admin(client, tenant_id, session_factory, "admin5@example.com")
    victim_id, victim_token, _ = await register_and_login(client, tenant_id, "deact@example.com")

    response = await client.post(
        f"/api/v1/users/{victim_id}/deactivate", headers=auth_header(admin_token)
    )
    assert response.status_code == 200
    assert response.json()["data"]["isActive"] is False

    me = await client.get("/api/v1/auth/me", headers=auth_header(victim_token))
    assert me.status_code == 401


async def test_delete_user_then_gone(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    admin_id, admin_token = await _admin(client, tenant_id, session_factory, "admin6@example.com")
    victim_id, victim_token, _ = await register_and_login(client, tenant_id, "victim@example.com")

    response = await client.delete(f"/api/v1/users/{victim_id}", headers=auth_header(admin_token))
    assert response.status_code == 204

    missing = await client.get(f"/api/v1/users/{victim_id}", headers=auth_header(admin_token))
    assert missing.status_code == 404

    me = await client.get("/api/v1/auth/me", headers=auth_header(victim_token))
    assert me.status_code == 401

    async with session_factory() as session:
        from app.modules.users.infrastructure.models import UserModel

        stmt = UserModel.__table__.select().where(UserModel.id == UUID(victim_id))
        row = (await session.execute(stmt)).first()
        assert row is not None
        assert row.deleted_at is not None
        assert row.deleted_by == UUID(admin_id)  # the acting admin, from the request context


async def test_delete_user_without_permission_forbidden(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    user_id, token, _ = await register_and_login(client, tenant_id, "noperm@example.com")
    await grant_role(
        session_factory,
        tenant_id=tenant_id,
        user_id=user_id,
        role_name="viewer",
        permissions=["users:read"],
    )

    response = await client.delete(f"/api/v1/users/{user_id}", headers=auth_header(token))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "insufficient_permission"


async def test_update_user_audit_event_has_before_and_after(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    admin_id, admin_token = await _admin(
        client, tenant_id, session_factory, "admin-audit1@example.com"
    )

    await client.patch(
        f"/api/v1/users/{admin_id}",
        json={"fullName": "Renamed"},
        headers=auth_header(admin_token),
    )

    (entry,) = await audit_entries(client, admin_token, "user.updated")
    assert entry["before"]["fullName"] == "Test User"
    assert entry["after"]["fullName"] == "Renamed"


async def test_deactivate_user_audit_event_has_before_and_after(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    admin_id, admin_token = await _admin(
        client, tenant_id, session_factory, "admin-audit2@example.com"
    )
    victim_id, _, _ = await register_and_login(client, tenant_id, "victim-audit2@example.com")

    await client.post(f"/api/v1/users/{victim_id}/deactivate", headers=auth_header(admin_token))

    (entry,) = await audit_entries(client, admin_token, "user.deactivated")
    assert entry["resourceId"] == victim_id
    assert entry["before"]["isActive"] is True
    assert entry["after"]["isActive"] is False
    assert admin_id  # keep the admin fixture referenced


async def test_delete_user_audit_event_has_before_snapshot_without_password(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, admin_token = await _admin(client, tenant_id, session_factory, "admin-audit3@example.com")
    victim_id, _, _ = await register_and_login(client, tenant_id, "victim-audit3@example.com")

    await client.delete(f"/api/v1/users/{victim_id}", headers=auth_header(admin_token))

    (entry,) = await audit_entries(client, admin_token, "user.deleted")
    assert entry["before"]["email"] == "victim-audit3@example.com"
    assert entry["after"] is None
    assert "hashedPassword" not in entry["before"]
    assert "hashed_password" not in entry["before"]


async def test_delete_unknown_user_returns_404_and_records_no_audit_event(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, admin_token = await _admin(client, tenant_id, session_factory, "admin-audit4@example.com")

    response = await client.delete(f"/api/v1/users/{uuid4()}", headers=auth_header(admin_token))

    assert response.status_code == 404
    assert await audit_entries(client, admin_token, "user.deleted") == []
