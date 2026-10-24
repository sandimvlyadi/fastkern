"""Session revocation over HTTP: logout kills the session's access token, refresh-token
reuse kills the token family, and `activeRole` is never stale."""

from uuid import UUID

import httpx
from httpx import AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.roles.infrastructure.models import UserRoleModel
from tests.helpers import PASSWORD, auth_header, grant_role, register_and_login

ME = "/api/v1/auth/me"
REFRESH = "/api/v1/auth/refresh"
LOGOUT = "/api/v1/auth/logout"
LOGIN = "/api/v1/auth/login"
SWITCH = "/api/v1/auth/switch-role"


async def _login(client: AsyncClient, tenant_id: str, email: str) -> tuple[str, str]:
    response = await client.post(
        LOGIN, json={"tenantId": tenant_id, "email": email, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    return data["accessToken"], data["refreshToken"]


async def _refresh(client: AsyncClient, refresh_token: str) -> httpx.Response:
    return await client.post(REFRESH, json={"refreshToken": refresh_token})


# --- Logout revokes the session -------------------------------------------------------------------


async def test_logout_invalidates_the_access_token_of_that_session_only(
    client: AsyncClient, tenant_id: str
) -> None:
    _, access_1, refresh_1 = await register_and_login(client, tenant_id, "d3@example.com")
    access_2, _ = await _login(client, tenant_id, "d3@example.com")  # a second login (session)
    assert (await client.get(ME, headers=auth_header(access_1))).status_code == 200

    logout = await client.post(LOGOUT, json={"refreshToken": refresh_1})

    assert logout.status_code == 204
    revoked = await client.get(ME, headers=auth_header(access_1))
    assert revoked.status_code == 401
    assert revoked.json()["error"]["code"] == "invalid_token"
    assert (await client.get(ME, headers=auth_header(access_2))).status_code == 200


async def test_logout_is_idempotent_and_a_second_logout_still_204(
    client: AsyncClient, tenant_id: str
) -> None:
    _, _, refresh = await register_and_login(client, tenant_id, "d3b@example.com")

    first = await client.post(LOGOUT, json={"refreshToken": refresh})
    second = await client.post(LOGOUT, json={"refreshToken": refresh})

    assert (first.status_code, second.status_code) == (204, 204)


async def test_a_logged_out_session_cannot_be_refreshed_back_to_life(
    client: AsyncClient, tenant_id: str
) -> None:
    _, _, refresh = await register_and_login(client, tenant_id, "d3c@example.com")
    await client.post(LOGOUT, json={"refreshToken": refresh})

    response = await _refresh(client, refresh)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "token_revoked"


async def test_switch_role_stays_in_the_session_so_logout_covers_every_token_of_it(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    user_id, access_1, refresh_1 = await register_and_login(client, tenant_id, "d3d@example.com")
    await grant_role(
        session_factory, tenant_id=tenant_id, user_id=user_id, role_name="ops", permissions=[]
    )
    switched = await client.post(SWITCH, json={"roleName": "ops"}, headers=auth_header(access_1))
    assert switched.status_code == 200, switched.text
    access_2 = switched.json()["data"]["accessToken"]
    refresh_2 = switched.json()["data"]["refreshToken"]

    await client.post(LOGOUT, json={"refreshToken": refresh_2})

    assert (await client.get(ME, headers=auth_header(access_1))).status_code == 401
    assert (await client.get(ME, headers=auth_header(access_2))).status_code == 401
    # switch-role does not revoke the pre-switch refresh token; the session denial must.
    old = await _refresh(client, refresh_1)
    assert old.status_code == 401
    assert old.json()["error"]["code"] == "token_revoked"


# --- Refresh-token reuse revokes the session ------------------------------------------------------


async def test_reusing_a_refresh_token_revokes_the_derived_tokens_too(
    client: AsyncClient, tenant_id: str
) -> None:
    _, _, refresh_a = await register_and_login(client, tenant_id, "d4@example.com")
    first = await _refresh(client, refresh_a)  # A -> B
    assert first.status_code == 200
    access_b = first.json()["data"]["accessToken"]
    refresh_b = first.json()["data"]["refreshToken"]
    assert (await client.get(ME, headers=auth_header(access_b))).status_code == 200

    reuse = await _refresh(client, refresh_a)  # A again
    assert reuse.status_code == 401
    assert reuse.json()["error"]["code"] == "token_revoked"

    assert (await _refresh(client, refresh_b)).status_code == 401
    assert (await client.get(ME, headers=auth_header(access_b))).status_code == 401


async def test_reuse_in_one_session_does_not_touch_another_session_of_the_user(
    client: AsyncClient, tenant_id: str
) -> None:
    _, _, refresh_a = await register_and_login(client, tenant_id, "d4b@example.com")
    access_other, refresh_other = await _login(client, tenant_id, "d4b@example.com")
    await _refresh(client, refresh_a)
    await _refresh(client, refresh_a)  # reuse -> session A revoked

    assert (await client.get(ME, headers=auth_header(access_other))).status_code == 200
    assert (await _refresh(client, refresh_other)).status_code == 200


async def test_garbage_and_foreign_refresh_tokens_revoke_nothing(
    client: AsyncClient, tenant_id: str
) -> None:
    _, access, _ = await register_and_login(client, tenant_id, "d4c@example.com")

    garbage = await _refresh(client, "not-a-jwt")

    assert garbage.status_code == 401
    assert garbage.json()["error"]["code"] == "invalid_token"
    assert (await client.get(ME, headers=auth_header(access))).status_code == 200


# --- activeRole is never stale --------------------------------------------------------------------


async def test_active_role_is_not_stale_after_the_role_is_revoked(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    user_id, access, _ = await register_and_login(client, tenant_id, "d5@example.com")
    await grant_role(
        session_factory, tenant_id=tenant_id, user_id=user_id, role_name="alpha", permissions=[]
    )
    beta_id = await grant_role(
        session_factory, tenant_id=tenant_id, user_id=user_id, role_name="beta", permissions=[]
    )
    switched = await client.post(SWITCH, json={"roleName": "beta"}, headers=auth_header(access))
    access_beta = switched.json()["data"]["accessToken"]
    refresh_beta = switched.json()["data"]["refreshToken"]
    me = await client.get(ME, headers=auth_header(access_beta))
    assert me.json()["data"]["activeRole"] == "beta"

    async with session_factory() as session:
        await session.execute(
            delete(UserRoleModel).where(
                UserRoleModel.user_id == UUID(user_id), UserRoleModel.role_id == UUID(beta_id)
            )
        )
        await session.commit()

    stale = await client.get(ME, headers=auth_header(access_beta))
    assert stale.json()["data"]["activeRole"] is None  # not advertising a role they lost

    refreshed = await _refresh(client, refresh_beta)
    assert refreshed.status_code == 200
    fresh_access = refreshed.json()["data"]["accessToken"]
    after = await client.get(ME, headers=auth_header(fresh_access))
    assert after.json()["data"]["activeRole"] == "alpha"  # fell back to a role still assigned
