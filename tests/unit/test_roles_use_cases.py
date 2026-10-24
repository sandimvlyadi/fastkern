from uuid import UUID, uuid4

import pytest

from app.core.pagination import OffsetParams
from app.modules.roles.application.dto import (
    CreateRoleInput,
    RoleAssignmentChange,
    UpdateRoleInput,
)
from app.modules.roles.application.use_cases import (
    AssignRoleToUserUseCase,
    CreateRoleUseCase,
    DeleteRoleUseCase,
    ListPermissionCatalogUseCase,
    ListRolesUseCase,
    ListUserRolesUseCase,
    ProvisionBaselineRolesUseCase,
    RevokeRoleFromUserUseCase,
    SetDefaultRoleUseCase,
    SwitchActiveRoleUseCase,
    UpdateRoleUseCase,
)
from app.modules.roles.domain.baseline import TENANT_ROLE_PERMISSIONS
from app.modules.roles.domain.entities import Role, UserRoleAssignment, missing_grants
from app.modules.roles.domain.exceptions import (
    PermissionEscalationError,
    RoleAlreadyExistsError,
    RoleNotAssignedError,
    RoleNotFoundError,
    SystemRoleImmutableError,
    UnknownPermissionError,
)
from app.modules.roles.domain.permissions import PERMISSION_CATALOG
from app.modules.users.domain.entities import User
from app.modules.users.domain.exceptions import UserNotFoundError
from tests.fakes import InMemoryRoleRepository, InMemoryUserRepository, InMemoryUserRoleRepository


async def _caller(*permissions: str) -> tuple[InMemoryUserRoleRepository, UUID]:
    """A caller who holds exactly one role granting `permissions`."""
    user_roles = InMemoryUserRoleRepository()
    role = Role(tenant_id=uuid4(), name=f"caller-{uuid4().hex[:6]}", permissions=list(permissions))
    user_roles.seed_role(role)
    caller_id = uuid4()
    await user_roles.assign(caller_id, role.id)
    return user_roles, caller_id


async def test_create_role_success() -> None:
    roles = InMemoryRoleRepository()
    user_roles, caller_id = await _caller("users:read")
    role = await CreateRoleUseCase(roles, user_roles).execute(
        CreateRoleInput(tenant_id=uuid4(), name="editor", permissions=["users:read"]), caller_id
    )
    assert role.name == "editor"


async def test_create_role_duplicate_name_raises() -> None:
    roles = InMemoryRoleRepository()
    user_roles, caller_id = await _caller("*")
    data = CreateRoleInput(tenant_id=uuid4(), name="editor")
    await CreateRoleUseCase(roles, user_roles).execute(data, caller_id)
    with pytest.raises(RoleAlreadyExistsError):
        await CreateRoleUseCase(roles, user_roles).execute(data, caller_id)


async def test_create_role_with_permission_caller_lacks_raises_and_adds_nothing() -> None:
    roles = InMemoryRoleRepository()
    user_roles, caller_id = await _caller("roles:write")

    with pytest.raises(PermissionEscalationError):
        await CreateRoleUseCase(roles, user_roles).execute(
            CreateRoleInput(tenant_id=uuid4(), name="sneaky", permissions=["roles:write", "*"]),
            caller_id,
        )

    assert roles.roles == {}


async def test_wildcard_caller_may_grant_anything_including_wildcard() -> None:
    roles = InMemoryRoleRepository()
    user_roles, caller_id = await _caller("*")
    role = await CreateRoleUseCase(roles, user_roles).execute(
        CreateRoleInput(tenant_id=uuid4(), name="root-ish", permissions=["*", "tenants:manage"]),
        caller_id,
    )
    assert role.permissions == ["*", "tenants:manage"]


def test_missing_grants_pools_all_of_the_holders_roles() -> None:
    a = Role(tenant_id=uuid4(), name="a", permissions=["x:read"])
    b = Role(tenant_id=uuid4(), name="b", permissions=["x:write"])
    assert missing_grants([a, b], ["x:read", "x:write", "x:delete"]) == ["x:delete"]
    assert missing_grants([], []) == []
    assert missing_grants([], ["x:read"]) == ["x:read"]


async def test_list_roles() -> None:
    roles = InMemoryRoleRepository()
    await roles.add(Role(tenant_id=uuid4(), name="a"))
    await roles.add(Role(tenant_id=uuid4(), name="b"))
    result = await ListRolesUseCase(roles).execute(OffsetParams())
    assert result.total == 2
    assert len(result.items) == 2


async def test_assign_role_unknown_role_raises() -> None:
    roles = InMemoryRoleRepository()
    user_roles = InMemoryUserRoleRepository()
    users = InMemoryUserRepository()
    user = User(tenant_id=uuid4(), email="a@example.com", hashed_password="h", full_name="A")
    await users.add(user)

    with pytest.raises(RoleNotFoundError):
        await AssignRoleToUserUseCase(roles, user_roles, users).execute(user.id, uuid4(), uuid4())


async def test_assign_role_unknown_user_raises() -> None:
    roles = InMemoryRoleRepository()
    user_roles = InMemoryUserRoleRepository()
    users = InMemoryUserRepository()
    role = await roles.add(Role(tenant_id=uuid4(), name="editor"))

    with pytest.raises(UserNotFoundError):
        await AssignRoleToUserUseCase(roles, user_roles, users).execute(uuid4(), role.id, uuid4())


async def test_assign_role_success() -> None:
    roles = InMemoryRoleRepository()
    user_roles = InMemoryUserRoleRepository()
    users = InMemoryUserRepository()
    user = User(tenant_id=uuid4(), email="a@example.com", hashed_password="h", full_name="A")
    await users.add(user)
    role = await roles.add(Role(tenant_id=user.tenant_id, name="editor"))
    user_roles.seed_role(role)

    change = await AssignRoleToUserUseCase(roles, user_roles, users).execute(
        user.id, role.id, uuid4()
    )

    assert change == RoleAssignmentChange(user_id=user.id, role_id=role.id, role_name="editor")
    assert (user.id, role.id) in user_roles.assignments


async def test_assign_role_already_assigned_reports_no_change() -> None:
    roles = InMemoryRoleRepository()
    user_roles = InMemoryUserRoleRepository()
    users = InMemoryUserRepository()
    user = User(tenant_id=uuid4(), email="a@example.com", hashed_password="h", full_name="A")
    await users.add(user)
    role = await roles.add(Role(tenant_id=user.tenant_id, name="editor"))
    user_roles.seed_role(role)
    await user_roles.assign(user.id, role.id)

    change = await AssignRoleToUserUseCase(roles, user_roles, users).execute(
        user.id, role.id, uuid4()
    )

    assert change is None


async def test_assign_first_role_becomes_default_but_second_does_not() -> None:
    roles = InMemoryRoleRepository()
    user_roles = InMemoryUserRoleRepository()
    users = InMemoryUserRepository()
    user = User(tenant_id=uuid4(), email="a@example.com", hashed_password="h", full_name="A")
    await users.add(user)
    editor = await roles.add(Role(tenant_id=user.tenant_id, name="editor"))
    viewer = await roles.add(Role(tenant_id=user.tenant_id, name="viewer"))
    user_roles.seed_role(editor)
    user_roles.seed_role(viewer)

    await AssignRoleToUserUseCase(roles, user_roles, users).execute(user.id, editor.id, uuid4())
    await AssignRoleToUserUseCase(roles, user_roles, users).execute(user.id, viewer.id, uuid4())

    default_role = await user_roles.get_default_role_for_user(user.id)
    assert default_role == editor


async def test_revoke_role_returns_removed_assignment() -> None:
    roles = InMemoryRoleRepository()
    user_roles = InMemoryUserRoleRepository()
    user_id = uuid4()
    role = await roles.add(Role(tenant_id=uuid4(), name="editor"))
    await user_roles.assign(user_id, role.id)

    change = await RevokeRoleFromUserUseCase(roles, user_roles).execute(user_id, role.id)

    assert change == RoleAssignmentChange(user_id=user_id, role_id=role.id, role_name="editor")
    assert (user_id, role.id) not in user_roles.assignments


async def test_revoke_role_not_assigned_reports_no_change() -> None:
    roles = InMemoryRoleRepository()
    role = await roles.add(Role(tenant_id=uuid4(), name="editor"))

    change = await RevokeRoleFromUserUseCase(roles, InMemoryUserRoleRepository()).execute(
        uuid4(), role.id
    )

    assert change is None


async def test_revoke_role_still_removes_assignment_when_role_no_longer_resolvable() -> None:
    # e.g. the role was soft-deleted: the role lookup returns None, but the
    # dangling assignment must still be removable (and audited, nameless).
    roles = InMemoryRoleRepository()
    user_roles = InMemoryUserRoleRepository()
    user_id, gone_role_id = uuid4(), uuid4()
    await user_roles.assign(user_id, gone_role_id)

    change = await RevokeRoleFromUserUseCase(roles, user_roles).execute(user_id, gone_role_id)

    assert change == RoleAssignmentChange(user_id=user_id, role_id=gone_role_id, role_name=None)
    assert (user_id, gone_role_id) not in user_roles.assignments


async def test_revoking_the_default_role_promotes_the_next_remaining_one() -> None:
    roles = InMemoryRoleRepository()
    user_roles = InMemoryUserRoleRepository()
    user_id = uuid4()
    admin = await roles.add(Role(tenant_id=uuid4(), name="admin"))
    editor = await roles.add(Role(tenant_id=uuid4(), name="editor"))
    user_roles.seed_role(admin)
    user_roles.seed_role(editor)
    await user_roles.assign(user_id, admin.id, is_default=True)
    await user_roles.assign(user_id, editor.id)

    await RevokeRoleFromUserUseCase(roles, user_roles).execute(user_id, admin.id)

    # "editor" sorts after "admin" alphabetically, but it's the only one left,
    # so it must be promoted to default rather than leaving the user with none.
    default_role = await user_roles.get_default_role_for_user(user_id)
    assert default_role == editor


async def test_revoking_a_non_default_role_leaves_the_existing_default_untouched() -> None:
    roles = InMemoryRoleRepository()
    user_roles = InMemoryUserRoleRepository()
    user_id = uuid4()
    admin = await roles.add(Role(tenant_id=uuid4(), name="admin"))
    editor = await roles.add(Role(tenant_id=uuid4(), name="editor"))
    user_roles.seed_role(admin)
    user_roles.seed_role(editor)
    await user_roles.assign(user_id, admin.id, is_default=True)
    await user_roles.assign(user_id, editor.id)

    await RevokeRoleFromUserUseCase(roles, user_roles).execute(user_id, editor.id)

    default_role = await user_roles.get_default_role_for_user(user_id)
    assert default_role == admin


async def test_revoking_the_last_role_leaves_no_default_to_promote() -> None:
    roles = InMemoryRoleRepository()
    user_roles = InMemoryUserRoleRepository()
    user_id = uuid4()
    admin = await roles.add(Role(tenant_id=uuid4(), name="admin"))
    user_roles.seed_role(admin)
    await user_roles.assign(user_id, admin.id, is_default=True)

    await RevokeRoleFromUserUseCase(roles, user_roles).execute(user_id, admin.id)

    assert await user_roles.get_default_role_for_user(user_id) is None


async def test_set_default_role_success() -> None:
    roles = InMemoryRoleRepository()
    user_roles = InMemoryUserRoleRepository()
    users = InMemoryUserRepository()
    user = User(tenant_id=uuid4(), email="setdef@example.com", hashed_password="h", full_name="U")
    await users.add(user)
    admin = await roles.add(Role(tenant_id=uuid4(), name="admin"))
    editor = await roles.add(Role(tenant_id=uuid4(), name="editor"))
    user_roles.seed_role(admin)
    user_roles.seed_role(editor)
    await user_roles.assign(user.id, admin.id, is_default=True)
    await user_roles.assign(user.id, editor.id)

    result = await SetDefaultRoleUseCase(roles, user_roles, users).execute(user.id, editor.id)

    assert result == editor
    assert await user_roles.get_default_role_for_user(user.id) == editor


async def test_set_default_role_not_assigned_raises() -> None:
    roles = InMemoryRoleRepository()
    user_roles = InMemoryUserRoleRepository()
    users = InMemoryUserRepository()
    user = User(
        tenant_id=uuid4(), email="notassigned@example.com", hashed_password="h", full_name="U"
    )
    await users.add(user)
    editor = await roles.add(Role(tenant_id=uuid4(), name="editor"))
    user_roles.seed_role(editor)

    with pytest.raises(RoleNotAssignedError):
        await SetDefaultRoleUseCase(roles, user_roles, users).execute(user.id, editor.id)


async def test_set_default_role_unknown_role_raises() -> None:
    roles = InMemoryRoleRepository()
    user_roles = InMemoryUserRoleRepository()
    users = InMemoryUserRepository()

    with pytest.raises(RoleNotFoundError):
        await SetDefaultRoleUseCase(roles, user_roles, users).execute(uuid4(), uuid4())


async def test_set_default_role_unknown_user_raises() -> None:
    roles = InMemoryRoleRepository()
    user_roles = InMemoryUserRoleRepository()
    users = InMemoryUserRepository()
    editor = await roles.add(Role(tenant_id=uuid4(), name="editor"))
    user_roles.seed_role(editor)

    with pytest.raises(UserNotFoundError):
        await SetDefaultRoleUseCase(roles, user_roles, users).execute(uuid4(), editor.id)


def test_role_assignment_change_audit_snapshot() -> None:
    user_id, role_id = uuid4(), uuid4()

    change = RoleAssignmentChange(user_id=user_id, role_id=role_id, role_name=None)

    snapshot = change.audit_snapshot()

    assert snapshot == {"userId": str(user_id), "roleId": str(role_id), "roleName": None}


async def test_list_user_roles() -> None:
    user_roles = InMemoryUserRoleRepository()
    user_id = uuid4()
    role = Role(tenant_id=uuid4(), name="editor")
    user_roles.seed_role(role)
    await user_roles.assign(user_id, role.id)

    result = await ListUserRolesUseCase(user_roles).execute(user_id, OffsetParams())

    assert result.items == [UserRoleAssignment(role=role, is_default=False)]


async def test_switch_active_role_found() -> None:
    user_roles = InMemoryUserRoleRepository()
    user_id = uuid4()
    role = Role(tenant_id=uuid4(), name="admin")
    user_roles.seed_role(role)
    await user_roles.assign(user_id, role.id)

    result = await SwitchActiveRoleUseCase(user_roles).execute(user_id, "admin")

    assert result == role


async def test_switch_active_role_missing_raises() -> None:
    user_roles = InMemoryUserRoleRepository()
    with pytest.raises(RoleNotAssignedError):
        await SwitchActiveRoleUseCase(user_roles).execute(uuid4(), "admin")


async def test_update_role_changes_fields_and_returns_before_after() -> None:
    roles = InMemoryRoleRepository()
    user_roles, caller_id = await _caller("users:write")
    role = await roles.add(
        Role(tenant_id=uuid4(), name="editor", description="old", permissions=["users:read"])
    )

    before, after = await UpdateRoleUseCase(roles, user_roles).execute(
        role.id,
        UpdateRoleInput(name="writer", description="new", permissions=["users:write"]),
        caller_id,
    )

    assert (before.name, before.description, before.permissions) == (
        "editor",
        "old",
        ["users:read"],
    )
    assert (after.name, after.description, after.permissions) == ("writer", "new", ["users:write"])


async def test_update_role_leaves_omitted_fields_unchanged() -> None:
    roles = InMemoryRoleRepository()
    user_roles, caller_id = await _caller()
    role = await roles.add(Role(tenant_id=uuid4(), name="editor", permissions=["users:read"]))

    _, after = await UpdateRoleUseCase(roles, user_roles).execute(
        role.id, UpdateRoleInput(description="d"), caller_id
    )

    assert after.name == "editor"
    assert after.permissions == ["users:read"]
    assert after.description == "d"


async def test_update_role_permissions_are_replaced_not_merged() -> None:
    roles = InMemoryRoleRepository()
    user_roles, caller_id = await _caller("posts:read")
    role = await roles.add(Role(tenant_id=uuid4(), name="editor", permissions=["a", "b"]))

    _, after = await UpdateRoleUseCase(roles, user_roles).execute(
        role.id, UpdateRoleInput(permissions=["posts:read"]), caller_id
    )

    assert after.permissions == ["posts:read"]


async def test_update_role_rename_to_existing_name_raises() -> None:
    roles = InMemoryRoleRepository()
    user_roles, caller_id = await _caller()
    tenant_id = uuid4()
    await roles.add(Role(tenant_id=tenant_id, name="taken"))
    role = await roles.add(Role(tenant_id=tenant_id, name="editor"))

    with pytest.raises(RoleAlreadyExistsError):
        await UpdateRoleUseCase(roles, user_roles).execute(
            role.id, UpdateRoleInput(name="taken"), caller_id
        )


async def test_update_role_keeping_its_own_name_is_not_a_conflict() -> None:
    roles = InMemoryRoleRepository()
    user_roles, caller_id = await _caller()
    role = await roles.add(Role(tenant_id=uuid4(), name="editor"))

    _, after = await UpdateRoleUseCase(roles, user_roles).execute(
        role.id, UpdateRoleInput(name="editor", description="d"), caller_id
    )

    assert after.name == "editor"


async def test_update_role_unknown_raises() -> None:
    user_roles, caller_id = await _caller()
    with pytest.raises(RoleNotFoundError):
        await UpdateRoleUseCase(InMemoryRoleRepository(), user_roles).execute(
            uuid4(), UpdateRoleInput(), caller_id
        )


async def test_update_system_role_raises_and_changes_nothing() -> None:
    roles = InMemoryRoleRepository()
    user_roles, caller_id = await _caller("*")
    role = await roles.add(Role(tenant_id=uuid4(), name="admin", permissions=["*"], is_system=True))

    with pytest.raises(SystemRoleImmutableError):
        await UpdateRoleUseCase(roles, user_roles).execute(
            role.id, UpdateRoleInput(permissions=[]), caller_id
        )

    assert roles.roles[role.id].permissions == ["*"]


async def test_update_role_adding_permission_caller_lacks_raises_and_changes_nothing() -> None:
    roles = InMemoryRoleRepository()
    user_roles, caller_id = await _caller("roles:write")
    role = await roles.add(Role(tenant_id=uuid4(), name="editor", permissions=["users:read"]))

    with pytest.raises(PermissionEscalationError):
        await UpdateRoleUseCase(roles, user_roles).execute(
            role.id, UpdateRoleInput(permissions=["users:read", "*"]), caller_id
        )

    assert roles.roles[role.id].permissions == ["users:read"]


async def test_update_role_may_keep_or_drop_permissions_the_caller_lacks() -> None:
    """Only *added* permissions are checked: keeping/dropping never widens access."""
    roles = InMemoryRoleRepository()
    user_roles, caller_id = await _caller("roles:write")
    role = await roles.add(Role(tenant_id=uuid4(), name="editor", permissions=["a", "b", "c"]))

    _, after = await UpdateRoleUseCase(roles, user_roles).execute(
        role.id, UpdateRoleInput(permissions=["a", "b"]), caller_id
    )

    assert after.permissions == ["a", "b"]


async def test_assign_role_granting_permission_caller_lacks_raises_and_assigns_nothing() -> None:
    roles = InMemoryRoleRepository()
    user_roles, caller_id = await _caller("roles:write")
    users = InMemoryUserRepository()
    user = User(tenant_id=uuid4(), email="a@example.com", hashed_password="h", full_name="A")
    await users.add(user)
    superadmin = await roles.add(
        Role(tenant_id=user.tenant_id, name="superadmin", permissions=["*"], is_system=True)
    )
    user_roles.seed_role(superadmin)

    with pytest.raises(PermissionEscalationError):
        await AssignRoleToUserUseCase(roles, user_roles, users).execute(
            user.id, superadmin.id, caller_id
        )

    assert (user.id, superadmin.id) not in user_roles.assignments


async def test_assign_role_within_callers_own_permissions_succeeds() -> None:
    roles = InMemoryRoleRepository()
    user_roles, caller_id = await _caller("roles:write", "users:read")
    users = InMemoryUserRepository()
    user = User(tenant_id=uuid4(), email="a@example.com", hashed_password="h", full_name="A")
    await users.add(user)
    role = await roles.add(
        Role(tenant_id=user.tenant_id, name="viewer", permissions=["users:read"])
    )
    user_roles.seed_role(role)

    change = await AssignRoleToUserUseCase(roles, user_roles, users).execute(
        user.id, role.id, caller_id
    )

    assert change is not None


async def test_provision_baseline_roles_creates_admin_and_user_as_system_roles() -> None:
    roles = InMemoryRoleRepository()
    tenant_id = uuid4()

    created = await ProvisionBaselineRolesUseCase(roles).execute(tenant_id)

    assert {r.name for r in created} == {"admin", "user"}
    by_name = {r.name: r for r in created}
    assert all(r.is_system and r.tenant_id == tenant_id for r in created)
    assert by_name["admin"].permissions == list(TENANT_ROLE_PERMISSIONS["admin"])
    assert by_name["user"].permissions == []
    # A tenant admin must never come out of provisioning able to manage other tenants.
    assert not any(p == "*" or p.startswith("tenants:") for p in by_name["admin"].permissions)


async def test_provision_baseline_roles_is_idempotent_and_keeps_existing_roles() -> None:
    roles = InMemoryRoleRepository()
    tenant_id = uuid4()
    custom_admin = await roles.add(Role(tenant_id=tenant_id, name="admin", permissions=["x:y"]))

    created = await ProvisionBaselineRolesUseCase(roles).execute(tenant_id)
    again = await ProvisionBaselineRolesUseCase(roles).execute(tenant_id)

    assert {r.name for r in created} == {"user"}  # the pre-existing admin was left alone
    assert again == []
    assert (await roles.get_by_name("admin", tenant_id=tenant_id)) == custom_admin


async def test_provision_baseline_roles_is_scoped_to_the_given_tenant() -> None:
    roles = InMemoryRoleRepository()
    first, second = uuid4(), uuid4()

    await ProvisionBaselineRolesUseCase(roles).execute(first)
    created_for_second = await ProvisionBaselineRolesUseCase(roles).execute(second)

    assert {r.name for r in created_for_second} == {"admin", "user"}


async def test_delete_role_returns_before_and_deletes() -> None:
    roles = InMemoryRoleRepository()
    role = await roles.add(Role(tenant_id=uuid4(), name="editor"))

    before = await DeleteRoleUseCase(roles).execute(role.id)

    assert before.name == "editor"
    assert roles.deleted_ids == [role.id]


async def test_delete_role_unknown_raises_and_deletes_nothing() -> None:
    roles = InMemoryRoleRepository()
    with pytest.raises(RoleNotFoundError):
        await DeleteRoleUseCase(roles).execute(uuid4())
    assert roles.deleted_ids == []


async def test_delete_system_role_raises_and_deletes_nothing() -> None:
    roles = InMemoryRoleRepository()
    role = await roles.add(Role(tenant_id=uuid4(), name="admin", is_system=True))

    with pytest.raises(SystemRoleImmutableError):
        await DeleteRoleUseCase(roles).execute(role.id)

    assert roles.deleted_ids == []


async def test_create_role_with_unknown_permission_raises_even_for_a_wildcard_caller() -> None:
    roles = InMemoryRoleRepository()
    user_roles, caller_id = await _caller("*")

    with pytest.raises(UnknownPermissionError):
        await CreateRoleUseCase(roles, user_roles).execute(
            CreateRoleInput(tenant_id=uuid4(), name="typo", permissions=["user:read"]), caller_id
        )

    assert roles.roles == {}


async def test_update_role_checks_only_added_permissions_against_the_catalog() -> None:
    roles = InMemoryRoleRepository()
    user_roles, caller_id = await _caller("*")
    role = await roles.add(Role(tenant_id=uuid4(), name="legacy", permissions=["retired:read"]))

    with pytest.raises(UnknownPermissionError):
        await UpdateRoleUseCase(roles, user_roles).execute(
            role.id, UpdateRoleInput(permissions=["retired:read", "nope:read"]), caller_id
        )
    _, after = await UpdateRoleUseCase(roles, user_roles).execute(
        role.id, UpdateRoleInput(permissions=["retired:read", "users:read"]), caller_id
    )

    assert after.permissions == ["retired:read", "users:read"]


async def test_permission_catalog_flags_grantable_and_hides_platform_only() -> None:
    user_roles, caller_id = await _caller("users:read", "roles:read")

    page = await ListPermissionCatalogUseCase(user_roles).execute(
        caller_id, OffsetParams(page_size=200)
    )

    by_code = {entry.code: entry for entry in page.items}
    assert {code for code, entry in by_code.items() if entry.grantable} == {
        "users:read",
        "roles:read",
    }
    assert not any(entry.platform_only for entry in page.items)
    assert "users:write" in by_code
    assert page.total == len(page.items)


async def test_permission_catalog_shows_platform_only_to_a_wildcard_caller() -> None:
    user_roles, caller_id = await _caller("*")

    page = await ListPermissionCatalogUseCase(user_roles).execute(
        caller_id, OffsetParams(page_size=200)
    )

    assert [entry.code for entry in page.items] == [p.code for p in PERMISSION_CATALOG]
    assert all(entry.grantable for entry in page.items)


async def test_permission_catalog_shows_a_platform_permission_its_literal_holder_can_grant() -> (
    None
):
    user_roles, caller_id = await _caller("tenants:manage")

    page = await ListPermissionCatalogUseCase(user_roles).execute(
        caller_id, OffsetParams(page_size=200)
    )

    platform = [entry.code for entry in page.items if entry.platform_only]
    assert platform == ["tenants:manage"]
