"""PATCH /users/{id} refuses locale/timezone values the app can't honour."""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.helpers import auth_header, grant_role, register_and_login


async def _admin(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> tuple[str, str]:
    user_id, token, _ = await register_and_login(client, tenant_id, "b4-admin@example.com")
    await grant_role(
        session_factory,
        tenant_id=tenant_id,
        user_id=user_id,
        role_name="admin",
        permissions=["users:read", "users:write"],
    )
    return user_id, token


@pytest.mark.parametrize(
    "payload",
    [{"locale": "xx"}, {"timezone": "Mars/Olympus"}, {"locale": "id", "timezone": "Nope/Zone"}],
)
async def test_invalid_locale_or_timezone_is_422_and_changes_nothing(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    payload: dict[str, str],
) -> None:
    user_id, token = await _admin(client, tenant_id, session_factory)
    before = (await client.get(f"/api/v1/users/{user_id}", headers=auth_header(token))).json()

    response = await client.patch(
        f"/api/v1/users/{user_id}", json=payload, headers=auth_header(token)
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    for bad in payload.values():
        if bad in {"xx", "Mars/Olympus", "Nope/Zone"}:
            assert bad not in response.text  # not reflected back
    after = (await client.get(f"/api/v1/users/{user_id}", headers=auth_header(token))).json()
    assert after["data"] == before["data"]


async def test_valid_locale_and_timezone_are_saved(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    user_id, token = await _admin(client, tenant_id, session_factory)

    response = await client.patch(
        f"/api/v1/users/{user_id}",
        json={"locale": "id", "timezone": "Asia/Jakarta"},
        headers=auth_header(token),
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert (data["locale"], data["timezone"]) == ("id", "Asia/Jakarta")
