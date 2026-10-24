"""`page` is bounded. Meant to run on Postgres too: SQLite happily accepts an OFFSET that
overflows int64, which is exactly why the 500 was never caught."""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.pagination import MAX_PAGE
from tests.helpers import auth_header, grant_role, register_and_login


async def _token(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    permissions: list[str],
    email: str,
) -> str:
    user_id, token, _ = await register_and_login(client, tenant_id, email)
    await grant_role(
        session_factory,
        tenant_id=tenant_id,
        user_id=user_id,
        role_name="pager",
        permissions=permissions,
    )
    return token


@pytest.mark.parametrize("page", [MAX_PAGE + 1, 10**30])
async def test_page_beyond_the_bound_is_422_not_500(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    page: int,
) -> None:
    token = await _token(client, tenant_id, session_factory, ["users:read"], "page-hi@example.com")

    response = await client.get(f"/api/v1/users?page={page}", headers=auth_header(token))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_page_at_the_bound_is_an_empty_page(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    token = await _token(client, tenant_id, session_factory, ["users:read"], "page-max@example.com")

    response = await client.get(f"/api/v1/users?page={MAX_PAGE}", headers=auth_header(token))

    assert response.status_code == 200
    assert response.json()["data"] == []
    assert response.json()["meta"]["pagination"]["page"] == MAX_PAGE


@pytest.mark.parametrize("path", ["/api/v1/roles", "/api/v1/audit-logs", "/api/v1/tenants"])
async def test_every_list_endpoint_shares_the_bound(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    path: str,
) -> None:
    token = await _token(
        client,
        tenant_id,
        session_factory,
        ["*"],
        f"page-{path.rsplit('/', 1)[-1]}@example.com",
    )

    response = await client.get(f"{path}?page=10000000000000000000000", headers=auth_header(token))

    assert response.status_code == 422
