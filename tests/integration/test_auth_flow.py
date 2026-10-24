from uuid import UUID, uuid4

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.roles.infrastructure.models import RoleModel, UserRoleModel
from app.modules.tenants.infrastructure.models import TenantModel
from app.modules.users.infrastructure.models import UserModel
from tests.helpers import audit_entries, create_role, grant_role, register_and_login


async def _register_and_login(
    client: AsyncClient, tenant_id: str, email: str = "user@example.com"
) -> dict:
    await client.post(
        "/api/v1/auth/register",
        json={
            "tenantId": tenant_id,
            "email": email,
            "password": "S3curePass!",
            "fullName": "Test User",
        },
    )
    response = await client.post(
        "/api/v1/auth/login",
        json={"tenantId": tenant_id, "email": email, "password": "S3curePass!"},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def test_register_login_me_flow(client: AsyncClient, tenant_id: str) -> None:
    tokens = await _register_and_login(client, tenant_id)
    assert tokens["accessToken"]
    assert tokens["refreshToken"]

    me_response = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {tokens['accessToken']}"}
    )
    assert me_response.status_code == 200
    assert me_response.json()["data"]["email"] == "user@example.com"


async def test_login_with_wrong_password_fails(client: AsyncClient, tenant_id: str) -> None:
    await client.post(
        "/api/v1/auth/register",
        json={
            "tenantId": tenant_id,
            "email": "wrongpass@example.com",
            "password": "S3curePass!",
            "fullName": "U",
        },
    )
    response = await client.post(
        "/api/v1/auth/login",
        json={
            "tenantId": tenant_id,
            "email": "wrongpass@example.com",
            "password": "not-the-password",
        },
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


async def test_refresh_token_rotates_and_invalidates_old_token(
    client: AsyncClient, tenant_id: str
) -> None:
    tokens = await _register_and_login(client, tenant_id, email="refresh@example.com")

    refresh_response = await client.post(
        "/api/v1/auth/refresh", json={"refreshToken": tokens["refreshToken"]}
    )
    assert refresh_response.status_code == 200
    new_tokens = refresh_response.json()["data"]
    assert new_tokens["refreshToken"] != tokens["refreshToken"]

    reuse_response = await client.post(
        "/api/v1/auth/refresh", json={"refreshToken": tokens["refreshToken"]}
    )
    assert reuse_response.status_code == 401


async def test_register_auto_assigns_default_user_role(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await create_role(session_factory, tenant_id=tenant_id, role_name="user", permissions=[])

    tokens = await _register_and_login(client, tenant_id, email="autorole@example.com")

    me_response = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {tokens['accessToken']}"}
    )
    assert me_response.status_code == 200
    assert me_response.json()["data"]["activeRole"] == "user"


async def test_register_without_default_role_still_succeeds(
    client: AsyncClient, tenant_id: str
) -> None:
    # No "user" role seeded for this tenant_id -> registration must not fail.
    tokens = await _register_and_login(client, tenant_id, email="norole@example.com")
    assert tokens["accessToken"]


async def test_register_unknown_tenant_is_rejected(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "tenantId": str(uuid4()),
            "email": "ghost@example.com",
            "password": "S3curePass!",
            "fullName": "Ghost",
        },
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "tenant_not_found"


async def _deactivate_tenant(
    session_factory: async_sessionmaker[AsyncSession], tenant_id: str
) -> None:
    async with session_factory() as session:
        tenant = await session.get(TenantModel, UUID(tenant_id))
        assert tenant is not None
        tenant.is_active = False
        await session.commit()


async def test_register_inactive_tenant_looks_like_unknown_tenant(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _deactivate_tenant(session_factory, tenant_id)

    response = await client.post(
        "/api/v1/auth/register",
        json={
            "tenantId": tenant_id,
            "email": "inactive@example.com",
            "password": "S3curePass!",
            "fullName": "Inactive",
        },
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "tenant_not_found"


async def test_login_and_refresh_are_blocked_once_tenant_is_deactivated(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tokens = await _register_and_login(client, tenant_id, email="suspended@example.com")
    await _deactivate_tenant(session_factory, tenant_id)

    login = await client.post(
        "/api/v1/auth/login",
        json={"tenantId": tenant_id, "email": "suspended@example.com", "password": "S3curePass!"},
    )
    assert login.status_code == 403
    assert login.json()["error"]["code"] == "tenant_inactive"

    bad_login = await client.post(
        "/api/v1/auth/login",
        json={"tenantId": tenant_id, "email": "suspended@example.com", "password": "nope"},
    )
    assert bad_login.status_code == 401
    assert bad_login.json()["error"]["code"] == "invalid_credentials"

    refresh = await client.post(
        "/api/v1/auth/refresh", json={"refreshToken": tokens["refreshToken"]}
    )
    assert refresh.status_code == 403
    assert refresh.json()["error"]["code"] == "tenant_inactive"


async def test_switch_role_requires_assigned_role(client: AsyncClient, tenant_id: str) -> None:
    tokens = await _register_and_login(client, tenant_id, email="switch@example.com")
    response = await client.post(
        "/api/v1/auth/switch-role",
        json={"roleName": "admin"},
        headers={"Authorization": f"Bearer {tokens['accessToken']}"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "role_not_assigned"


async def test_switch_role_succeeds_once_assigned(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tokens = await _register_and_login(client, tenant_id, email="admin-switch@example.com")

    async with session_factory() as session:
        user_row = (
            await session.execute(
                UserModel.__table__.select().where(UserModel.email == "admin-switch@example.com")
            )
        ).first()
        role = RoleModel(tenant_id=UUID(tenant_id), name="admin", permissions=["*"])
        session.add(role)
        await session.flush()
        session.add(UserRoleModel(tenant_id=UUID(tenant_id), user_id=user_row.id, role_id=role.id))
        await session.commit()

    response = await client.post(
        "/api/v1/auth/switch-role",
        json={"roleName": "admin"},
        headers={"Authorization": f"Bearer {tokens['accessToken']}"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["accessToken"]


async def test_my_roles_lists_assigned_roles_with_default_flag(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tokens = await _register_and_login(client, tenant_id, email="myroles@example.com")
    me_response = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {tokens['accessToken']}"}
    )
    user_id = me_response.json()["data"]["id"]

    async with session_factory() as session:
        admin_role = RoleModel(tenant_id=UUID(tenant_id), name="my-roles-admin", permissions=["*"])
        viewer_role = RoleModel(tenant_id=UUID(tenant_id), name="my-roles-viewer", permissions=[])
        session.add_all([admin_role, viewer_role])
        await session.flush()
        session.add(
            UserRoleModel(
                tenant_id=UUID(tenant_id),
                user_id=UUID(user_id),
                role_id=admin_role.id,
                is_default=True,
            )
        )
        session.add(
            UserRoleModel(
                tenant_id=UUID(tenant_id),
                user_id=UUID(user_id),
                role_id=viewer_role.id,
                is_default=False,
            )
        )
        await session.commit()

    response = await client.get(
        "/api/v1/auth/me/roles", headers={"Authorization": f"Bearer {tokens['accessToken']}"}
    )

    assert response.status_code == 200, response.text
    by_name = {r["name"]: r["isDefault"] for r in response.json()["data"]}
    assert by_name == {"my-roles-admin": True, "my-roles-viewer": False}


async def test_my_roles_requires_authentication(client: AsyncClient) -> None:
    response = await client.get("/api/v1/auth/me/roles")
    assert response.status_code in (401, 403)


async def test_register_and_login_are_audited_as_the_user(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    user_id, token, _ = await register_and_login(client, tenant_id, "audited@example.com")
    await grant_role(
        session_factory,
        tenant_id=tenant_id,
        user_id=user_id,
        role_name="auditor",
        permissions=["audit_log:read"],
    )

    (registered,) = await audit_entries(client, token, "user.registered")
    assert registered["actorId"] == user_id
    assert registered["before"] is None
    assert registered["after"] == {
        "email": "audited@example.com",
        "fullName": "Test User",
        "isActive": True,
        "locale": "en",
        "timezone": "UTC",
    }
    (logged_in,) = await audit_entries(client, token, "auth.login")
    assert logged_in["actorId"] == user_id
    assert logged_in["resourceId"] == user_id


async def test_self_registration_leaves_actor_columns_null(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """No authenticated user exists while registering, so created_by/updated_by
    stay NULL by design; who registered is answered by the
    `user.registered` audit event (actor = the new user), asserted above."""
    user_id, _, _ = await register_and_login(client, tenant_id, "selfsignup@example.com")
    async with session_factory() as session:
        row = (
            await session.execute(select(UserModel).where(UserModel.id == UUID(user_id)))
        ).scalar_one()
    assert (row.created_by, row.updated_by, row.deleted_by) == (None, None, None)


async def test_failed_login_is_not_audited(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    user_id, token, _ = await register_and_login(client, tenant_id, "failed@example.com")
    await grant_role(
        session_factory,
        tenant_id=tenant_id,
        user_id=user_id,
        role_name="auditor",
        permissions=["audit_log:read"],
    )

    bad = await client.post(
        "/api/v1/auth/login",
        json={"tenantId": tenant_id, "email": "failed@example.com", "password": "wrong-password"},
    )

    assert bad.status_code == 401
    assert len(await audit_entries(client, token, "auth.login")) == 1  # only the successful one
