from uuid import UUID, uuid4

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.roles.infrastructure.models import RoleModel, UserRoleModel
from tests.helpers import (
    audit_entries,
    auth_header,
    create_role,
    grant_role,
    register_and_login,
)


async def _admin(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    email: str,
) -> tuple[str, str]:
    user_id, token, _ = await register_and_login(client, tenant_id, email)
    await grant_role(
        session_factory, tenant_id=tenant_id, user_id=user_id, role_name="admin", permissions=["*"]
    )
    return user_id, token


async def test_list_roles_requires_permission(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, no_perm_token, _ = await register_and_login(client, tenant_id, "noperm@example.com")
    forbidden = await client.get("/api/v1/roles", headers=auth_header(no_perm_token))
    assert forbidden.status_code == 403

    _, admin_token = await _admin(client, tenant_id, session_factory, "admin@example.com")
    allowed = await client.get("/api/v1/roles", headers=auth_header(admin_token))
    assert allowed.status_code == 200
    assert isinstance(allowed.json()["data"], list)


async def test_create_role_success_records_audit_and_uses_context_tenant(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, admin_token = await _admin(client, tenant_id, session_factory, "admin2@example.com")

    response = await client.post(
        "/api/v1/roles",
        json={"name": "editor", "description": "Editor role", "permissions": ["users:read"]},
        headers=auth_header(admin_token),
    )
    assert response.status_code == 201, response.text
    body = response.json()["data"]
    assert body["name"] == "editor"
    assert body["tenantId"] == tenant_id

    logs = await client.get("/api/v1/audit-logs", headers=auth_header(admin_token))
    actions = {entry["action"] for entry in logs.json()["data"]}
    assert "role.created" in actions


async def test_create_role_duplicate_name_conflict(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, admin_token = await _admin(client, tenant_id, session_factory, "admin3@example.com")
    payload = {"name": "duplicate-role", "permissions": []}
    await client.post("/api/v1/roles", json=payload, headers=auth_header(admin_token))

    response = await client.post("/api/v1/roles", json=payload, headers=auth_header(admin_token))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "role_already_exists"


async def test_create_role_empty_name_is_rejected(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, admin_token = await _admin(client, tenant_id, session_factory, "admin4@example.com")
    response = await client.post(
        "/api/v1/roles", json={"name": "", "permissions": []}, headers=auth_header(admin_token)
    )
    assert response.status_code == 422


async def test_assign_and_list_and_revoke_role(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    admin_id, admin_token = await _admin(client, tenant_id, session_factory, "admin5@example.com")
    target_id, _, _ = await register_and_login(client, tenant_id, "target@example.com")

    created = await client.post(
        "/api/v1/roles",
        json={"name": "assignable", "permissions": []},
        headers=auth_header(admin_token),
    )
    role_id = created.json()["data"]["id"]

    assign = await client.post(
        f"/api/v1/roles/users/{target_id}",
        json={"roleId": role_id},
        headers=auth_header(admin_token),
    )
    assert assign.status_code == 204

    listed = await client.get(f"/api/v1/roles/users/{target_id}", headers=auth_header(admin_token))
    assert any(r["id"] == role_id for r in listed.json()["data"])

    revoke = await client.delete(
        f"/api/v1/roles/users/{target_id}/{role_id}", headers=auth_header(admin_token)
    )
    assert revoke.status_code == 204

    after_revoke = await client.get(
        f"/api/v1/roles/users/{target_id}", headers=auth_header(admin_token)
    )
    assert all(r["id"] != role_id for r in after_revoke.json()["data"])
    assert admin_id  # keep the admin fixture referenced


async def test_first_assigned_role_is_default_second_is_not(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, admin_token = await _admin(client, tenant_id, session_factory, "admin-default@example.com")
    target_id, _, _ = await register_and_login(client, tenant_id, "default-target@example.com")
    first = await client.post(
        "/api/v1/roles", json={"name": "default-first"}, headers=auth_header(admin_token)
    )
    second = await client.post(
        "/api/v1/roles", json={"name": "default-second"}, headers=auth_header(admin_token)
    )
    first_id, second_id = first.json()["data"]["id"], second.json()["data"]["id"]

    await client.post(
        f"/api/v1/roles/users/{target_id}",
        json={"roleId": first_id},
        headers=auth_header(admin_token),
    )
    await client.post(
        f"/api/v1/roles/users/{target_id}",
        json={"roleId": second_id},
        headers=auth_header(admin_token),
    )

    listed = await client.get(f"/api/v1/roles/users/{target_id}", headers=auth_header(admin_token))
    by_id = {r["id"]: r["isDefault"] for r in listed.json()["data"]}
    assert by_id == {first_id: True, second_id: False}


async def test_set_default_role_changes_default_and_is_audited(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, admin_token = await _admin(client, tenant_id, session_factory, "admin-setdef@example.com")
    target_id, _, _ = await register_and_login(client, tenant_id, "setdef-target@example.com")
    first = await client.post(
        "/api/v1/roles", json={"name": "setdef-first"}, headers=auth_header(admin_token)
    )
    second = await client.post(
        "/api/v1/roles", json={"name": "setdef-second"}, headers=auth_header(admin_token)
    )
    first_id, second_id = first.json()["data"]["id"], second.json()["data"]["id"]
    for role_id in (first_id, second_id):
        await client.post(
            f"/api/v1/roles/users/{target_id}",
            json={"roleId": role_id},
            headers=auth_header(admin_token),
        )

    response = await client.post(
        f"/api/v1/roles/users/{target_id}/{second_id}/default", headers=auth_header(admin_token)
    )
    assert response.status_code == 204

    listed = await client.get(f"/api/v1/roles/users/{target_id}", headers=auth_header(admin_token))
    by_id = {r["id"]: r["isDefault"] for r in listed.json()["data"]}
    assert by_id == {first_id: False, second_id: True}

    (entry,) = await audit_entries(client, admin_token, "role.default_set")
    assert entry["before"] == {"defaultRole": "setdef-first"}
    assert entry["after"] == {"defaultRole": "setdef-second"}


async def test_set_default_role_not_assigned_is_forbidden(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, admin_token = await _admin(client, tenant_id, session_factory, "admin-nosetdef@example.com")
    target_id, _, _ = await register_and_login(client, tenant_id, "nosetdef-target@example.com")
    never_assigned = await client.post(
        "/api/v1/roles", json={"name": "never-assigned-role"}, headers=auth_header(admin_token)
    )
    role_id = never_assigned.json()["data"]["id"]

    response = await client.post(
        f"/api/v1/roles/users/{target_id}/{role_id}/default", headers=auth_header(admin_token)
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "role_not_assigned"


async def test_set_default_role_requires_roles_write_permission(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, admin_token = await _admin(client, tenant_id, session_factory, "admin-nowrite@example.com")
    target_id, _, _ = await register_and_login(client, tenant_id, "nowrite-target@example.com")
    role = await client.post(
        "/api/v1/roles", json={"name": "nowrite-role"}, headers=auth_header(admin_token)
    )
    role_id = role.json()["data"]["id"]
    await client.post(
        f"/api/v1/roles/users/{target_id}",
        json={"roleId": role_id},
        headers=auth_header(admin_token),
    )
    _, no_perm_token, _ = await register_and_login(client, tenant_id, "nowrite-caller@example.com")

    response = await client.post(
        f"/api/v1/roles/users/{target_id}/{role_id}/default", headers=auth_header(no_perm_token)
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "insufficient_permission"


async def test_assign_unknown_role_returns_404(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, admin_token = await _admin(client, tenant_id, session_factory, "admin6@example.com")
    target_id, _, _ = await register_and_login(client, tenant_id, "target2@example.com")

    response = await client.post(
        f"/api/v1/roles/users/{target_id}",
        json={"roleId": str(uuid4())},
        headers=auth_header(admin_token),
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "role_not_found"


async def test_create_role_audit_event_has_after_snapshot(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, admin_token = await _admin(client, tenant_id, session_factory, "admin-audit1@example.com")

    await client.post(
        "/api/v1/roles",
        json={"name": "auditor", "description": "Reads", "permissions": ["users:read"]},
        headers=auth_header(admin_token),
    )

    (entry,) = await audit_entries(client, admin_token, "role.created")
    assert entry["before"] is None
    assert entry["after"] == {
        "name": "auditor",
        "description": "Reads",
        "permissions": ["users:read"],
        "isSystem": False,
    }


async def test_assign_and_revoke_role_audit_events_carry_role_details(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, admin_token = await _admin(client, tenant_id, session_factory, "admin-audit2@example.com")
    target_id, _, _ = await register_and_login(client, tenant_id, "target-audit2@example.com")
    created = await client.post(
        "/api/v1/roles",
        json={"name": "assignable-audit", "permissions": []},
        headers=auth_header(admin_token),
    )
    role_id = created.json()["data"]["id"]
    expected = {"userId": target_id, "roleId": role_id, "roleName": "assignable-audit"}

    await client.post(
        f"/api/v1/roles/users/{target_id}",
        json={"roleId": role_id},
        headers=auth_header(admin_token),
    )
    await client.delete(
        f"/api/v1/roles/users/{target_id}/{role_id}", headers=auth_header(admin_token)
    )

    (assigned,) = await audit_entries(client, admin_token, "role.assigned")
    assert assigned["before"] is None
    assert assigned["after"] == expected
    (revoked,) = await audit_entries(client, admin_token, "role.revoked")
    assert revoked["before"] == expected
    assert revoked["after"] is None


async def test_noop_assign_and_revoke_record_no_audit_event(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, admin_token = await _admin(client, tenant_id, session_factory, "admin-audit3@example.com")
    target_id, _, _ = await register_and_login(client, tenant_id, "target-audit3@example.com")
    created = await client.post(
        "/api/v1/roles",
        json={"name": "noop-audit", "permissions": []},
        headers=auth_header(admin_token),
    )
    role_id = created.json()["data"]["id"]

    # Revoking a role the user never had changes nothing...
    never_had = await client.delete(
        f"/api/v1/roles/users/{target_id}/{role_id}", headers=auth_header(admin_token)
    )
    # ...and assigning the same role twice only changes state the first time.
    for _ in range(2):
        await client.post(
            f"/api/v1/roles/users/{target_id}",
            json={"roleId": role_id},
            headers=auth_header(admin_token),
        )

    assert never_had.status_code == 204
    assert await audit_entries(client, admin_token, "role.revoked") == []
    assert len(await audit_entries(client, admin_token, "role.assigned")) == 1


async def test_revoke_cleans_up_assignment_to_soft_deleted_role_and_audits_it(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, admin_token = await _admin(client, tenant_id, session_factory, "admin-audit4@example.com")
    target_id, _, _ = await register_and_login(client, tenant_id, "target-audit4@example.com")
    created = await client.post(
        "/api/v1/roles",
        json={"name": "doomed-audit", "permissions": []},
        headers=auth_header(admin_token),
    )
    role_id = created.json()["data"]["id"]
    await client.post(
        f"/api/v1/roles/users/{target_id}",
        json={"roleId": role_id},
        headers=auth_header(admin_token),
    )
    async with session_factory() as session:  # no delete-role endpoint yet
        role = (
            await session.execute(select(RoleModel).where(RoleModel.id == UUID(role_id)))
        ).scalar_one()
        role.soft_delete()
        await session.commit()

    revoke = await client.delete(
        f"/api/v1/roles/users/{target_id}/{role_id}", headers=auth_header(admin_token)
    )

    assert revoke.status_code == 204
    async with session_factory() as session:
        rows = await session.execute(
            select(UserRoleModel).where(UserRoleModel.user_id == UUID(target_id))
        )
        assert rows.scalars().all() == []
    (entry,) = await audit_entries(client, admin_token, "role.revoked")
    assert entry["before"] == {"userId": target_id, "roleId": role_id, "roleName": None}


async def _system_role(session_factory: async_sessionmaker[AsyncSession], tenant_id: str) -> str:
    async with session_factory() as session:
        role = RoleModel(
            tenant_id=UUID(tenant_id),
            name=f"sys-{uuid4().hex[:8]}",
            permissions=["users:read"],
            is_system=True,
        )
        session.add(role)
        await session.commit()
        return str(role.id)


async def test_update_role_success_records_before_and_after(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, admin_token = await _admin(client, tenant_id, session_factory, "upd-admin@example.com")
    created = await client.post(
        "/api/v1/roles",
        json={"name": "upd-editor", "description": "old", "permissions": ["users:read"]},
        headers=auth_header(admin_token),
    )
    role_id = created.json()["data"]["id"]

    response = await client.patch(
        f"/api/v1/roles/{role_id}",
        json={"name": "upd-writer", "permissions": ["users:write"]},
        headers=auth_header(admin_token),
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert (data["name"], data["description"], data["permissions"]) == (
        "upd-writer",
        "old",
        ["users:write"],
    )
    (entry,) = await audit_entries(client, admin_token, "role.updated")
    assert entry["before"]["name"] == "upd-editor"
    assert entry["after"]["name"] == "upd-writer"


async def test_update_role_duplicate_name_conflict(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, admin_token = await _admin(client, tenant_id, session_factory, "upd-dup@example.com")
    await client.post("/api/v1/roles", json={"name": "upd-taken"}, headers=auth_header(admin_token))
    other = await client.post(
        "/api/v1/roles", json={"name": "upd-other"}, headers=auth_header(admin_token)
    )

    response = await client.patch(
        f"/api/v1/roles/{other.json()['data']['id']}",
        json={"name": "upd-taken"},
        headers=auth_header(admin_token),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "role_already_exists"


async def test_update_unknown_role_is_404(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, admin_token = await _admin(client, tenant_id, session_factory, "upd-404@example.com")
    response = await client.patch(
        f"/api/v1/roles/{uuid4()}", json={"name": "x"}, headers=auth_header(admin_token)
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "role_not_found"


async def test_update_and_delete_system_role_are_forbidden(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, admin_token = await _admin(client, tenant_id, session_factory, "sys-admin@example.com")
    role_id = await _system_role(session_factory, tenant_id)

    patched = await client.patch(
        f"/api/v1/roles/{role_id}", json={"permissions": []}, headers=auth_header(admin_token)
    )
    deleted = await client.delete(f"/api/v1/roles/{role_id}", headers=auth_header(admin_token))

    for response in (patched, deleted):
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "system_role_immutable"


async def test_delete_role_requires_roles_delete_permission(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    user_id, token, _ = await register_and_login(client, tenant_id, "del-noperm@example.com")
    await grant_role(
        session_factory,
        tenant_id=tenant_id,
        user_id=user_id,
        role_name="writer-only",
        permissions=["roles:read", "roles:write"],
    )
    _, admin_token = await _admin(client, tenant_id, session_factory, "del-admin0@example.com")
    created = await client.post(
        "/api/v1/roles", json={"name": "del-target0"}, headers=auth_header(admin_token)
    )

    response = await client.delete(
        f"/api/v1/roles/{created.json()['data']['id']}", headers=auth_header(token)
    )

    assert response.status_code == 403


async def test_delete_role_soft_deletes_audits_and_frees_the_name(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, admin_token = await _admin(client, tenant_id, session_factory, "del-admin@example.com")
    created = await client.post(
        "/api/v1/roles", json={"name": "del-target"}, headers=auth_header(admin_token)
    )
    role_id = created.json()["data"]["id"]

    deleted = await client.delete(f"/api/v1/roles/{role_id}", headers=auth_header(admin_token))
    assert deleted.status_code == 204

    (entry,) = await audit_entries(client, admin_token, "role.deleted")
    assert entry["before"]["name"] == "del-target"
    assert entry["resourceId"] == role_id

    again = await client.patch(
        f"/api/v1/roles/{role_id}", json={"name": "x"}, headers=auth_header(admin_token)
    )
    assert again.status_code == 404

    async with session_factory() as session:
        row = (
            await session.execute(select(RoleModel).where(RoleModel.id == UUID(role_id)))
        ).scalar_one()
        assert row.deleted_at is not None  # soft-deleted, not removed

    # The soft-deleted name is free again (partial unique index).
    recreated = await client.post(
        "/api/v1/roles", json={"name": "del-target"}, headers=auth_header(admin_token)
    )
    assert recreated.status_code == 201, recreated.text


async def test_delete_unknown_role_is_404(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, admin_token = await _admin(client, tenant_id, session_factory, "del-404@example.com")
    response = await client.delete(f"/api/v1/roles/{uuid4()}", headers=auth_header(admin_token))
    assert response.status_code == 404


async def test_update_role_description_over_column_limit_is_rejected(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, admin_token = await _admin(client, tenant_id, session_factory, "upd-long@example.com")
    created = await client.post(
        "/api/v1/roles", json={"name": "upd-long-role"}, headers=auth_header(admin_token)
    )

    response = await client.patch(
        f"/api/v1/roles/{created.json()['data']['id']}",
        json={"description": "x" * 501},
        headers=auth_header(admin_token),
    )

    assert response.status_code == 422


async def _limited_writer(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    email: str,
) -> tuple[str, str]:
    """A `roles:write` holder who is NOT a wildcard holder: (user_id, token)."""
    user_id, token, _ = await register_and_login(client, tenant_id, email)
    await grant_role(
        session_factory,
        tenant_id=tenant_id,
        user_id=user_id,
        role_name="role-writer",
        permissions=["roles:read", "roles:write", "users:read"],
    )
    return user_id, token


async def test_create_role_cannot_grant_permissions_the_caller_lacks(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, token = await _limited_writer(client, tenant_id, session_factory, "esc-create@example.com")

    escalated = await client.post(
        "/api/v1/roles",
        json={"name": "esc-root", "permissions": ["*"]},
        headers=auth_header(token),
    )
    within_reach = await client.post(
        "/api/v1/roles",
        json={"name": "esc-ok", "permissions": ["users:read"]},
        headers=auth_header(token),
    )

    assert escalated.status_code == 403
    assert escalated.json()["error"]["code"] == "permission_escalation"
    assert within_reach.status_code == 201, within_reach.text


async def test_update_role_cannot_add_permissions_the_caller_lacks(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, token = await _limited_writer(client, tenant_id, session_factory, "esc-update@example.com")
    created = await client.post(
        "/api/v1/roles",
        json={"name": "esc-target", "permissions": ["users:read"]},
        headers=auth_header(token),
    )
    role_id = created.json()["data"]["id"]

    escalated = await client.patch(
        f"/api/v1/roles/{role_id}",
        json={"permissions": ["users:read", "users:delete"]},
        headers=auth_header(token),
    )
    narrowed = await client.patch(
        f"/api/v1/roles/{role_id}", json={"permissions": []}, headers=auth_header(token)
    )

    assert escalated.status_code == 403
    assert escalated.json()["error"]["code"] == "permission_escalation"
    assert narrowed.status_code == 200


async def test_assign_role_cannot_grant_permissions_the_caller_lacks(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    user_id, token = await _limited_writer(
        client, tenant_id, session_factory, "esc-assign@example.com"
    )
    root_role_id = await create_role(
        session_factory, tenant_id=tenant_id, role_name="esc-superadmin", permissions=["*"]
    )

    response = await client.post(
        f"/api/v1/roles/users/{user_id}",
        json={"roleId": root_role_id},
        headers=auth_header(token),
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_escalation"
    listed = await client.get(f"/api/v1/roles/users/{user_id}", headers=auth_header(token))
    assert all(r["id"] != root_role_id for r in listed.json()["data"])


async def test_noop_role_update_is_not_audited(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, admin_token = await _admin(client, tenant_id, session_factory, "noop-admin@example.com")
    created = await client.post(
        "/api/v1/roles", json={"name": "noop-role"}, headers=auth_header(admin_token)
    )

    response = await client.patch(
        f"/api/v1/roles/{created.json()['data']['id']}", json={}, headers=auth_header(admin_token)
    )

    assert response.status_code == 200
    assert await audit_entries(client, admin_token, "role.updated") == []


async def test_create_role_description_over_column_limit_is_rejected(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, admin_token = await _admin(client, tenant_id, session_factory, "create-long@example.com")
    response = await client.post(
        "/api/v1/roles",
        json={"name": "long-desc", "description": "x" * 501},
        headers=auth_header(admin_token),
    )
    assert response.status_code == 422
