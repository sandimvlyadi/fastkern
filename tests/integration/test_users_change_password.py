from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.helpers import PASSWORD, audit_entries, auth_header, grant_role, register_and_login


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


async def test_change_my_password_success_revokes_refresh_token_and_new_password_works(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    user_id, token, refresh_token = await register_and_login(
        client, tenant_id, "self-change@example.com"
    )

    response = await client.post(
        "/api/v1/users/me/password",
        json={"currentPassword": PASSWORD, "newPassword": "BrandNewP4ss!"},
        headers=auth_header(token),
    )
    assert response.status_code == 204, response.text

    # The refresh token minted before the change can no longer mint a new access token --
    # `revoke_all_for_user` revokes stored refresh tokens, not the (already-issued,
    # short-lived) access token itself, same as any other password-reset flow here.
    stale_refresh = await client.post("/api/v1/auth/refresh", json={"refreshToken": refresh_token})
    assert stale_refresh.status_code == 401

    # Logging in with the old password fails; the new one works.
    old_login = await client.post(
        "/api/v1/auth/login",
        json={"tenantId": tenant_id, "email": "self-change@example.com", "password": PASSWORD},
    )
    assert old_login.status_code == 401

    new_login = await client.post(
        "/api/v1/auth/login",
        json={
            "tenantId": tenant_id,
            "email": "self-change@example.com",
            "password": "BrandNewP4ss!",
        },
    )
    assert new_login.status_code == 200, new_login.text
    assert user_id  # keep the registered id referenced


async def test_change_my_password_wrong_current_password_returns_403(
    client: AsyncClient, tenant_id: str
) -> None:
    _, token, refresh_token = await register_and_login(
        client, tenant_id, "wrong-current@example.com"
    )

    response = await client.post(
        "/api/v1/users/me/password",
        json={"currentPassword": "NotMyPassword!", "newPassword": "BrandNewP4ss!"},
        headers=auth_header(token),
    )

    # 403, not 401: the bearer token itself is valid -- see IncorrectCurrentPasswordError.
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "incorrect_current_password"

    # A failed attempt must not revoke anything: the access token and the refresh token
    # (which the success test proves gets revoked) both still work.
    me = await client.get("/api/v1/auth/me", headers=auth_header(token))
    assert me.status_code == 200
    still_valid_refresh = await client.post(
        "/api/v1/auth/refresh", json={"refreshToken": refresh_token}
    )
    assert still_valid_refresh.status_code == 200


async def test_change_my_password_too_short_returns_422(
    client: AsyncClient, tenant_id: str
) -> None:
    _, token, _ = await register_and_login(client, tenant_id, "short-pass@example.com")

    response = await client.post(
        "/api/v1/users/me/password",
        json={"currentPassword": PASSWORD, "newPassword": "short"},
        headers=auth_header(token),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_change_my_password_records_audit_event_without_password(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    admin_id, admin_token = await _admin(
        client, tenant_id, session_factory, "self-audit@example.com"
    )

    await client.post(
        "/api/v1/users/me/password",
        json={"currentPassword": PASSWORD, "newPassword": "BrandNewP4ss!"},
        headers=auth_header(admin_token),
    )

    # admin_token's session was just revoked by the change above, so re-authenticate to read
    # the audit log as the same user with the new password.
    login = await client.post(
        "/api/v1/auth/login",
        json={
            "tenantId": tenant_id,
            "email": "self-audit@example.com",
            "password": "BrandNewP4ss!",
        },
    )
    fresh_token = login.json()["data"]["accessToken"]

    (entry,) = await audit_entries(client, fresh_token, "user.password_changed")
    assert entry["resourceId"] == admin_id
    assert entry["before"] is None
    assert entry["after"] is None


async def test_admin_set_password_via_patch_revokes_target_refresh_token(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, admin_token = await _admin(client, tenant_id, session_factory, "admin-pw@example.com")
    victim_id, _, victim_refresh = await register_and_login(
        client, tenant_id, "victim-pw@example.com"
    )

    response = await client.patch(
        f"/api/v1/users/{victim_id}",
        json={"password": "AdminSetP4ss!"},
        headers=auth_header(admin_token),
    )
    assert response.status_code == 200, response.text

    # Same contract as the self-service change: the old refresh token can no longer mint a
    # new access token.
    stale_refresh = await client.post("/api/v1/auth/refresh", json={"refreshToken": victim_refresh})
    assert stale_refresh.status_code == 401

    new_login = await client.post(
        "/api/v1/auth/login",
        json={
            "tenantId": tenant_id,
            "email": "victim-pw@example.com",
            "password": "AdminSetP4ss!",
        },
    )
    assert new_login.status_code == 200, new_login.text


async def test_admin_set_password_records_dedicated_audit_action_without_leaking_it(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, admin_token = await _admin(client, tenant_id, session_factory, "admin-pw-audit@example.com")
    victim_id, _, _ = await register_and_login(client, tenant_id, "victim-pw-audit@example.com")

    await client.patch(
        f"/api/v1/users/{victim_id}",
        json={"fullName": "Renamed", "password": "AdminSetP4ss!"},
        headers=auth_header(admin_token),
    )

    # The password reset is its own, dedicated, filterable action -- not folded into
    # "user.updated" -- and carries no before/after (nothing about a password is safe to log).
    (reset_entry,) = await audit_entries(client, admin_token, "user.password_reset_by_admin")
    assert reset_entry["resourceId"] == victim_id
    assert reset_entry["before"] is None
    assert reset_entry["after"] is None

    # The profile-field change is still recorded separately, and no longer carries a
    # passwordChanged flag now that the password has its own action.
    (update_entry,) = await audit_entries(client, admin_token, "user.updated")
    assert update_entry["resourceId"] == victim_id
    assert update_entry["after"]["fullName"] == "Renamed"
    assert "passwordChanged" not in update_entry["after"]


async def test_admin_set_password_without_permission_forbidden(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    user_id, token, _ = await register_and_login(client, tenant_id, "noperm-pw@example.com")
    await grant_role(
        session_factory,
        tenant_id=tenant_id,
        user_id=user_id,
        role_name="viewer",
        permissions=["users:read"],
    )
    victim_id, _, _ = await register_and_login(client, tenant_id, "victim-noperm@example.com")

    response = await client.patch(
        f"/api/v1/users/{victim_id}",
        json={"password": "ShouldNotApply1!"},
        headers=auth_header(token),
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "insufficient_permission"
