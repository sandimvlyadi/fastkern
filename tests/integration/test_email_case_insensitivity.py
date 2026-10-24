"""Email is case-insensitive as an identity: canonical on the way in, enforced by the DB."""

from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.users.domain.email import normalize_email
from app.modules.users.infrastructure.models import UserModel
from tests.helpers import HASHED_PASSWORD, PASSWORD, auth_header

REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"


def _register(tenant_id: str, email: str) -> dict[str, str]:
    return {"tenantId": tenant_id, "email": email, "password": PASSWORD, "fullName": "Case Test"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Jane@Example.COM", "jane@example.com"),
        ("  jane@example.com \n", "jane@example.com"),
        ("already@lower.io", "already@lower.io"),
        ("ÄÖ@Example.com", "äö@example.com"),
    ],
)
def test_normalize_email(raw: str, expected: str) -> None:
    assert normalize_email(raw) == expected


async def test_registration_stores_the_email_lowercased(
    client: AsyncClient, tenant_id: str
) -> None:
    response = await client.post(REGISTER, json=_register(tenant_id, "Case.X@Example.com"))

    assert response.status_code == 201, response.text
    assert response.json()["data"]["email"] == "case.x@example.com"


async def test_a_differently_cased_second_registration_is_a_conflict(
    client: AsyncClient, tenant_id: str
) -> None:
    first = await client.post(REGISTER, json=_register(tenant_id, "Case.X@Example.com"))
    second = await client.post(REGISTER, json=_register(tenant_id, "case.x@example.com"))
    third = await client.post(REGISTER, json=_register(tenant_id, "CASE.X@EXAMPLE.COM"))

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "email_already_registered"
    assert third.status_code == 409


@pytest.mark.parametrize(
    "login_email", ["CASE.X@EXAMPLE.COM", "case.x@example.com", "Case.X@Example.com"]
)
async def test_login_ignores_email_case(
    client: AsyncClient, tenant_id: str, login_email: str
) -> None:
    await client.post(REGISTER, json=_register(tenant_id, "case.x@example.com"))

    response = await client.post(
        LOGIN, json={"tenantId": tenant_id, "email": login_email, "password": PASSWORD}
    )

    assert response.status_code == 200, response.text
    token = response.json()["data"]["accessToken"]
    me = await client.get("/api/v1/auth/me", headers=auth_header(token))
    assert me.json()["data"]["email"] == "case.x@example.com"


async def test_the_database_itself_refuses_a_non_lowercase_email(
    session_factory: async_sessionmaker[AsyncSession], tenant_id: str
) -> None:
    async with session_factory() as session:
        session.add(
            UserModel(
                tenant_id=UUID(tenant_id),
                email=f"Upper-{uuid4().hex[:6]}@Example.com",
                hashed_password=HASHED_PASSWORD,
                full_name="Bypass",
            )
        )
        with pytest.raises(IntegrityError, match="ck_sys_users_email_lower"):
            await session.flush()
        await session.rollback()
