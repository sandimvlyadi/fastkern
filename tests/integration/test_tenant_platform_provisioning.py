"""Tenant management is platform-only and new tenants come with baseline roles.

The roles here are built from `roles.domain.baseline` -- the same definitions the seed script
and tenant provisioning use -- so this exercises what a *seeded* `admin`/`superadmin` can do,
not a hand-picked permission list."""

from uuid import UUID, uuid4

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.roles.domain.baseline import PLATFORM_ROLE_PERMISSIONS, TENANT_ROLE_PERMISSIONS
from app.modules.roles.infrastructure.models import RoleModel
from tests.helpers import audit_entries, auth_header, create_tenant, grant_role, register_and_login


async def _headers_with(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    role_name: str,
    permissions: tuple[str, ...],
) -> dict[str, str]:
    email = f"{role_name}-{uuid4().hex[:8]}@example.com"
    user_id, token, _ = await register_and_login(client, tenant_id, email)
    await grant_role(
        session_factory,
        tenant_id=tenant_id,
        user_id=user_id,
        role_name=role_name,
        permissions=list(permissions),
    )
    return auth_header(token)


# --- a tenant admin cannot manage tenants ---------------------------------------------------------


async def test_tenant_admin_cannot_list_create_update_suspend_or_delete_tenants(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    admin = await _headers_with(
        client,
        tenant_id,
        session_factory,
        role_name="admin",
        permissions=TENANT_ROLE_PERMISSIONS["admin"],
    )
    other_tenant = await create_tenant(session_factory)

    assert (await client.get("/api/v1/tenants", headers=admin)).status_code == 403
    assert (
        await client.post(
            "/api/v1/tenants",
            json={"name": "Mine", "slug": f"mine-{uuid4().hex[:8]}"},
            headers=admin,
        )
    ).status_code == 403
    assert (
        await client.patch(
            f"/api/v1/tenants/{other_tenant}", json={"name": "HACKED"}, headers=admin
        )
    ).status_code == 403
    assert (
        await client.post(f"/api/v1/tenants/{other_tenant}/deactivate", headers=admin)
    ).status_code == 403
    assert (
        await client.delete(f"/api/v1/tenants/{other_tenant}", headers=admin)
    ).status_code == 403


async def test_tenant_admin_cannot_mint_a_role_that_manages_tenants(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    admin = await _headers_with(
        client,
        tenant_id,
        session_factory,
        role_name="admin",
        permissions=TENANT_ROLE_PERMISSIONS["admin"],
    )

    response = await client.post(
        "/api/v1/roles", json={"name": "sneaky", "permissions": ["tenants:manage"]}, headers=admin
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_escalation"


async def test_platform_superadmin_can_still_manage_tenants(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    superadmin = await _headers_with(
        client,
        tenant_id,
        session_factory,
        role_name="superadmin",
        permissions=PLATFORM_ROLE_PERMISSIONS["superadmin"],
    )

    listing = await client.get("/api/v1/tenants", headers=superadmin)
    created = await client.post(
        "/api/v1/tenants",
        json={"name": "Acme", "slug": f"acme-{uuid4().hex[:8]}"},
        headers=superadmin,
    )

    assert listing.status_code == 200
    assert created.status_code == 201


# --- a new tenant gets its baseline roles ---------------------------------------------------------


async def _create_tenant_as_superadmin(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    slug: str | None = None,
) -> tuple[dict[str, str], str]:
    superadmin = await _headers_with(
        client,
        tenant_id,
        session_factory,
        role_name="superadmin",
        permissions=PLATFORM_ROLE_PERMISSIONS["superadmin"],
    )
    response = await client.post(
        "/api/v1/tenants",
        json={"name": "Provisioned", "slug": slug or f"prov-{uuid4().hex[:8]}"},
        headers=superadmin,
    )
    assert response.status_code == 201, response.text
    return superadmin, response.json()["data"]["id"]


async def test_created_tenant_has_admin_and_user_system_roles(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, new_tenant_id = await _create_tenant_as_superadmin(client, tenant_id, session_factory)

    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(RoleModel).where(RoleModel.tenant_id == UUID(new_tenant_id))
                )
            )
            .scalars()
            .all()
        )
    by_name = {r.name: r for r in rows}

    assert set(by_name) == {"admin", "user"}
    assert all(r.is_system for r in rows)
    assert by_name["admin"].permissions == list(TENANT_ROLE_PERMISSIONS["admin"])
    assert by_name["user"].permissions == []


async def test_self_registration_into_a_new_tenant_gets_the_user_role(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, new_tenant_id = await _create_tenant_as_superadmin(client, tenant_id, session_factory)

    _, token, _ = await register_and_login(client, new_tenant_id, "first-member@example.com")

    me = await client.get("/api/v1/auth/me", headers=auth_header(token))
    assert me.json()["data"]["activeRole"] == "user"  # no more `default_role_missing`


async def test_tenant_created_audit_event_is_still_recorded(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    superadmin, new_tenant_id = await _create_tenant_as_superadmin(
        client, tenant_id, session_factory
    )

    entries = await audit_entries(client, superadmin["Authorization"].split()[1], "tenant.created")

    assert [e["resourceId"] for e in entries] == [new_tenant_id]


async def test_duplicate_slug_creates_no_second_set_of_roles(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    # The slug is chosen here, not looked up in `GET /tenants`: that listing is platform-wide and
    # paginated, so on a database that accumulates tenants the new one can fall off page 1.
    slug = f"dup-{uuid4().hex[:8]}"
    superadmin, new_tenant_id = await _create_tenant_as_superadmin(
        client, tenant_id, session_factory, slug=slug
    )

    duplicate = await client.post(
        "/api/v1/tenants", json={"name": "Again", "slug": slug}, headers=superadmin
    )

    assert duplicate.status_code == 409
    async with session_factory() as session:
        count = len(
            (
                await session.execute(
                    select(RoleModel).where(RoleModel.tenant_id == UUID(new_tenant_id))
                )
            )
            .scalars()
            .all()
        )
    assert count == 2
