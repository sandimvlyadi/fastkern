from uuid import uuid4

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.helpers import (
    PASSWORD,
    audit_entries,
    auth_header,
    create_tenant,
    grant_role,
    register_and_login,
)


async def _admin(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    email: str,
) -> str:
    user_id, token, _ = await register_and_login(client, tenant_id, email)
    await grant_role(
        session_factory,
        tenant_id=tenant_id,
        user_id=user_id,
        role_name="platform-admin",
        permissions=["tenants:manage", "audit_log:read"],
    )
    return token


async def _super_admin(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    email: str,
) -> str:
    """Like `_admin`, but also holds `tenants:delete` (which `admin` deliberately lacks)."""
    user_id, token, _ = await register_and_login(client, tenant_id, email)
    await grant_role(
        session_factory,
        tenant_id=tenant_id,
        user_id=user_id,
        role_name="platform-super",
        permissions=["tenants:manage", "tenants:delete", "audit_log:read"],
    )
    return token


async def test_list_tenants_requires_permission(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, no_perm_token, _ = await register_and_login(client, tenant_id, "noperm@example.com")
    forbidden = await client.get("/api/v1/tenants", headers=auth_header(no_perm_token))
    assert forbidden.status_code == 403


async def test_create_tenant_success_and_appears_in_list(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    admin_token = await _admin(client, tenant_id, session_factory, "admin@example.com")
    slug = f"acme-{uuid4().hex[:8]}"

    created = await client.post(
        "/api/v1/tenants", json={"name": "Acme", "slug": slug}, headers=auth_header(admin_token)
    )
    assert created.status_code == 201, created.text
    assert created.json()["data"]["slug"] == slug

    logs = await client.get("/api/v1/audit-logs", headers=auth_header(admin_token))
    actions = {entry["action"] for entry in logs.json()["data"]}
    assert "tenant.created" in actions

    # Walk every page: the list has no guaranteed order, and the test database keeps every
    # tenant the suite has created so far, so the new one can be on any page.
    slugs: set[str] = set()
    page, total_pages = 1, 1
    while page <= total_pages:
        listed = await client.get(
            f"/api/v1/tenants?page={page}&page_size=200", headers=auth_header(admin_token)
        )
        slugs |= {t["slug"] for t in listed.json()["data"]}
        total_pages = listed.json()["meta"]["pagination"]["totalPages"]
        page += 1
    assert slug in slugs


async def test_create_tenant_duplicate_slug_conflict(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    admin_token = await _admin(client, tenant_id, session_factory, "admin2@example.com")
    slug = f"dup-{uuid4().hex[:8]}"
    payload = {"name": "Acme", "slug": slug}
    await client.post("/api/v1/tenants", json=payload, headers=auth_header(admin_token))

    response = await client.post("/api/v1/tenants", json=payload, headers=auth_header(admin_token))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "tenant_slug_already_exists"


async def test_create_tenant_invalid_slug_is_rejected(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    admin_token = await _admin(client, tenant_id, session_factory, "admin3@example.com")
    response = await client.post(
        "/api/v1/tenants",
        json={"name": "Acme", "slug": "Bad Slug"},
        headers=auth_header(admin_token),
    )
    assert response.status_code == 422


async def test_create_tenant_audit_event_has_after_snapshot(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    admin_token = await _admin(client, tenant_id, session_factory, "admin-audit@example.com")
    slug = f"audit-{uuid4().hex[:8]}"

    await client.post(
        "/api/v1/tenants", json={"name": "Audited", "slug": slug}, headers=auth_header(admin_token)
    )

    (entry,) = await audit_entries(client, admin_token, "tenant.created")
    assert entry["before"] is None
    assert entry["after"] == {"name": "Audited", "slug": slug, "isActive": True}


async def test_update_tenant_success_and_audit(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    admin_token = await _admin(client, tenant_id, session_factory, "tupd-admin@example.com")
    target_id = await create_tenant(session_factory)
    new_slug = f"renamed-{uuid4().hex[:8]}"

    response = await client.patch(
        f"/api/v1/tenants/{target_id}",
        json={"name": "Renamed", "slug": new_slug},
        headers=auth_header(admin_token),
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["name"] == "Renamed"
    assert response.json()["data"]["slug"] == new_slug
    (entry,) = await audit_entries(client, admin_token, "tenant.updated")
    assert entry["before"]["name"] == "Test Tenant"
    assert entry["after"] == {"name": "Renamed", "slug": new_slug, "isActive": True}


async def test_update_tenant_duplicate_slug_conflict_and_unknown_404(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    admin_token = await _admin(client, tenant_id, session_factory, "tupd-dup@example.com")
    taken_slug = f"taken-{uuid4().hex[:8]}"
    await client.post(
        "/api/v1/tenants",
        json={"name": "Taken", "slug": taken_slug},
        headers=auth_header(admin_token),
    )
    target_id = await create_tenant(session_factory)

    conflict = await client.patch(
        f"/api/v1/tenants/{target_id}",
        json={"slug": taken_slug},
        headers=auth_header(admin_token),
    )
    missing = await client.patch(
        f"/api/v1/tenants/{uuid4()}", json={"name": "x"}, headers=auth_header(admin_token)
    )

    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "tenant_slug_already_exists"
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "tenant_not_found"


async def test_deactivate_blocks_login_and_activate_restores_it(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    admin_token = await _super_admin(client, tenant_id, session_factory, "tact-admin@example.com")
    target_id = await create_tenant(session_factory)
    await register_and_login(client, target_id, "member@example.com")
    credentials = {"tenantId": target_id, "email": "member@example.com", "password": PASSWORD}

    deactivated = await client.post(
        f"/api/v1/tenants/{target_id}/deactivate", headers=auth_header(admin_token)
    )
    assert deactivated.status_code == 200, deactivated.text
    assert deactivated.json()["data"]["isActive"] is False
    blocked = await client.post("/api/v1/auth/login", json=credentials)
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "tenant_inactive"

    activated = await client.post(
        f"/api/v1/tenants/{target_id}/activate", headers=auth_header(admin_token)
    )
    assert activated.status_code == 200, activated.text
    assert activated.json()["data"]["isActive"] is True
    restored = await client.post("/api/v1/auth/login", json=credentials)
    assert restored.status_code == 200

    (deact,) = await audit_entries(client, admin_token, "tenant.deactivated")
    assert deact["before"]["isActive"] is True
    assert deact["after"]["isActive"] is False
    assert len(await audit_entries(client, admin_token, "tenant.activated")) == 1


async def test_deactivate_own_tenant_is_forbidden(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    admin_token = await _super_admin(client, tenant_id, session_factory, "tself-admin@example.com")
    response = await client.post(
        f"/api/v1/tenants/{tenant_id}/deactivate", headers=auth_header(admin_token)
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "cannot_modify_own_tenant"


async def test_delete_tenant_requires_tenants_delete_permission(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    admin_token = await _admin(client, tenant_id, session_factory, "tdel-noperm@example.com")
    target_id = await create_tenant(session_factory)

    response = await client.delete(f"/api/v1/tenants/{target_id}", headers=auth_header(admin_token))

    assert response.status_code == 403


async def test_delete_tenant_soft_deletes_audits_and_frees_the_slug(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    token = await _super_admin(client, tenant_id, session_factory, "tdel-super@example.com")
    slug = f"doomed-{uuid4().hex[:8]}"
    created = await client.post(
        "/api/v1/tenants", json={"name": "Doomed", "slug": slug}, headers=auth_header(token)
    )
    target_id = created.json()["data"]["id"]

    deleted = await client.delete(f"/api/v1/tenants/{target_id}", headers=auth_header(token))
    assert deleted.status_code == 204

    (entry,) = await audit_entries(client, token, "tenant.deleted")
    assert entry["before"]["slug"] == slug
    gone = await client.patch(
        f"/api/v1/tenants/{target_id}", json={"name": "x"}, headers=auth_header(token)
    )
    assert gone.status_code == 404

    # The soft-deleted slug is free again (partial unique index).
    recreated = await client.post(
        "/api/v1/tenants", json={"name": "Reborn", "slug": slug}, headers=auth_header(token)
    )
    assert recreated.status_code == 201, recreated.text


async def test_delete_own_tenant_is_forbidden_and_unknown_is_404(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    token = await _super_admin(client, tenant_id, session_factory, "tdel-self@example.com")

    own = await client.delete(f"/api/v1/tenants/{tenant_id}", headers=auth_header(token))
    missing = await client.delete(f"/api/v1/tenants/{uuid4()}", headers=auth_header(token))

    assert own.status_code == 403
    assert own.json()["error"]["code"] == "cannot_modify_own_tenant"
    assert missing.status_code == 404


async def test_deactivate_and_activate_require_tenants_delete_permission(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """`tenants:manage` alone (a narrower platform role) must not suspend or un-suspend tenants."""
    admin_token = await _admin(client, tenant_id, session_factory, "tgate-admin@example.com")
    target_id = await create_tenant(session_factory)

    deactivate = await client.post(
        f"/api/v1/tenants/{target_id}/deactivate", headers=auth_header(admin_token)
    )
    activate = await client.post(
        f"/api/v1/tenants/{target_id}/activate", headers=auth_header(admin_token)
    )

    assert deactivate.status_code == 403
    assert activate.status_code == 403


async def test_deactivating_a_tenant_cuts_existing_access_tokens(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    token = await _super_admin(client, tenant_id, session_factory, "tcut-super@example.com")
    target_id = await create_tenant(session_factory)
    _, member_token, _ = await register_and_login(client, target_id, "tcut-member@example.com")
    assert (
        await client.get("/api/v1/auth/me", headers=auth_header(member_token))
    ).status_code == 200

    await client.post(f"/api/v1/tenants/{target_id}/deactivate", headers=auth_header(token))
    suspended = await client.get("/api/v1/auth/me", headers=auth_header(member_token))
    assert suspended.status_code == 401

    await client.post(f"/api/v1/tenants/{target_id}/activate", headers=auth_header(token))
    restored = await client.get("/api/v1/auth/me", headers=auth_header(member_token))
    assert restored.status_code == 200


async def test_deleting_a_tenant_cuts_existing_access_tokens(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    token = await _super_admin(client, tenant_id, session_factory, "tcutdel-super@example.com")
    target_id = await create_tenant(session_factory)
    _, member_token, _ = await register_and_login(client, target_id, "tcutdel-member@example.com")

    await client.delete(f"/api/v1/tenants/{target_id}", headers=auth_header(token))

    response = await client.get("/api/v1/auth/me", headers=auth_header(member_token))
    assert response.status_code == 401


async def test_noop_tenant_changes_are_not_audited(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    token = await _super_admin(client, tenant_id, session_factory, "tnoop-super@example.com")
    target_id = await create_tenant(session_factory)

    await client.patch(f"/api/v1/tenants/{target_id}", json={}, headers=auth_header(token))
    await client.post(
        f"/api/v1/tenants/{target_id}/activate", headers=auth_header(token)
    )  # already active
    await client.post(f"/api/v1/tenants/{target_id}/deactivate", headers=auth_header(token))
    await client.post(
        f"/api/v1/tenants/{target_id}/deactivate", headers=auth_header(token)
    )  # again

    assert await audit_entries(client, token, "tenant.updated") == []
    assert await audit_entries(client, token, "tenant.activated") == []
    assert len(await audit_entries(client, token, "tenant.deactivated")) == 1
