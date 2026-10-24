from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.helpers import PASSWORD, auth_header, grant_role, register_and_login


async def test_register_duplicate_email_conflict(client: AsyncClient, tenant_id: str) -> None:
    payload = {
        "tenantId": tenant_id,
        "email": "dup@example.com",
        "password": PASSWORD,
        "fullName": "A",
    }
    await client.post("/api/v1/auth/register", json=payload)
    response = await client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "email_already_registered"


async def test_register_short_password_is_rejected(client: AsyncClient, tenant_id: str) -> None:
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "tenantId": tenant_id,
            "email": "short@example.com",
            "password": "x",
            "fullName": "A",
        },
    )
    assert response.status_code == 422


async def test_logout_then_refresh_is_revoked(client: AsyncClient, tenant_id: str) -> None:
    _, _, refresh_token = await register_and_login(client, tenant_id, "logout@example.com")

    logout = await client.post("/api/v1/auth/logout", json={"refreshToken": refresh_token})
    assert logout.status_code == 204

    refresh = await client.post("/api/v1/auth/refresh", json={"refreshToken": refresh_token})
    assert refresh.status_code == 401
    assert refresh.json()["error"]["code"] == "token_revoked"


async def test_logout_with_garbage_token_is_a_noop(client: AsyncClient) -> None:
    response = await client.post("/api/v1/auth/logout", json={"refreshToken": "garbage"})
    assert response.status_code == 204


async def test_refresh_with_garbage_token_rejected(client: AsyncClient) -> None:
    response = await client.post("/api/v1/auth/refresh", json={"refreshToken": "garbage"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


async def test_refresh_with_access_token_rejected(client: AsyncClient, tenant_id: str) -> None:
    _, access_token, _ = await register_and_login(client, tenant_id, "wrongtype@example.com")
    response = await client.post("/api/v1/auth/refresh", json={"refreshToken": access_token})
    assert response.status_code == 401


async def test_login_disabled_account_is_forbidden_until_reactivated(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    admin_id, admin_token, _ = await register_and_login(client, tenant_id, "the-admin@example.com")
    await grant_role(
        session_factory, tenant_id=tenant_id, user_id=admin_id, role_name="admin", permissions=["*"]
    )
    target_id, _, _ = await register_and_login(client, tenant_id, "disable-me@example.com")
    login = {"tenantId": tenant_id, "email": "disable-me@example.com", "password": PASSWORD}
    admin = auth_header(admin_token)

    await client.post(f"/api/v1/users/{target_id}/deactivate", headers=admin)
    disabled = await client.post("/api/v1/auth/login", json=login)
    # 403: right credentials, refused account -- not 401, which would mean "retry".
    assert disabled.status_code == 403
    assert disabled.json()["error"]["code"] == "account_disabled"

    await client.post(f"/api/v1/users/{target_id}/activate", headers=admin)
    restored = await client.post("/api/v1/auth/login", json=login)
    assert restored.status_code == 200


async def test_me_with_bad_token_rejected(client: AsyncClient) -> None:
    response = await client.get("/api/v1/auth/me", headers=auth_header("not-a-jwt"))
    assert response.status_code == 401


async def test_me_with_negotiated_headers(client: AsyncClient, tenant_id: str) -> None:
    _, access_token, _ = await register_and_login(client, tenant_id, "headers@example.com")
    response = await client.get(
        "/api/v1/auth/me",
        headers={**auth_header(access_token), "Accept-Language": "id-ID", "X-Timezone": "UTC"},
    )
    assert response.status_code == 200
