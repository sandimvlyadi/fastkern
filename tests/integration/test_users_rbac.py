from uuid import UUID

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.roles.infrastructure.models import RoleModel, UserRoleModel
from app.modules.users.infrastructure.models import UserModel


async def _register_and_login(client: AsyncClient, tenant_id: str, email: str) -> str:
    await client.post(
        "/api/v1/auth/register",
        json={
            "tenantId": tenant_id,
            "email": email,
            "password": "S3curePass!",
            "fullName": "RBAC User",
        },
    )
    response = await client.post(
        "/api/v1/auth/login",
        json={"tenantId": tenant_id, "email": email, "password": "S3curePass!"},
    )
    return response.json()["data"]["accessToken"]  # type: ignore[no-any-return]


async def test_list_users_requires_authentication(client: AsyncClient) -> None:
    response = await client.get("/api/v1/users")
    assert response.status_code in (401, 403)


async def test_list_users_forbidden_without_permission(client: AsyncClient, tenant_id: str) -> None:
    token = await _register_and_login(client, tenant_id, "no-perm@example.com")
    response = await client.get("/api/v1/users", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "insufficient_permission"


async def test_list_users_allowed_with_permission(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    token = await _register_and_login(client, tenant_id, "has-perm@example.com")

    async with session_factory() as session:
        user_row = (
            await session.execute(
                UserModel.__table__.select().where(UserModel.email == "has-perm@example.com")
            )
        ).first()
        role = RoleModel(tenant_id=UUID(tenant_id), name="viewer", permissions=["users:read"])
        session.add(role)
        await session.flush()
        session.add(UserRoleModel(tenant_id=UUID(tenant_id), user_id=user_row.id, role_id=role.id))
        await session.commit()

    response = await client.get("/api/v1/users", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200, response.text
    assert isinstance(response.json()["data"], list)
