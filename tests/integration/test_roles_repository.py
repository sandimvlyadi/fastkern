from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import update_request_context
from app.core.pagination import OffsetParams
from app.modules.roles.domain.entities import Role, UserRoleAssignment
from app.modules.roles.domain.exceptions import RoleAlreadyExistsError
from app.modules.roles.infrastructure.models import UserRoleModel
from app.modules.roles.infrastructure.repository import RoleRepository, UserRoleRepository
from app.shared.infrastructure.repository import MissingTenantScopeError
from tests.helpers import TenantFactory, UserFactory


async def test_role_repository_add_and_get_by_name(
    db_session: AsyncSession, create_db_tenant: TenantFactory
) -> None:
    roles = RoleRepository(db_session)
    tenant_id = await create_db_tenant()
    update_request_context(tenant_id=tenant_id)

    await roles.add(Role(tenant_id=tenant_id, name="editor", permissions=["users:read"]))

    found = await roles.get_by_name("editor")
    assert found is not None
    assert found.permissions == ["users:read"]
    assert await roles.get_by_name("missing") is None


async def test_role_repository_list_all_is_tenant_scoped(
    db_session: AsyncSession, create_db_tenant: TenantFactory
) -> None:
    roles = RoleRepository(db_session)
    tenant_a, tenant_b = await create_db_tenant(), await create_db_tenant()

    update_request_context(tenant_id=tenant_a)
    await roles.add(Role(tenant_id=tenant_a, name="a-role"))

    update_request_context(tenant_id=tenant_b)
    await roles.add(Role(tenant_id=tenant_b, name="b-role"))

    all_for_b = await roles.list_all()
    assert [r.name for r in all_for_b] == ["b-role"]


async def test_user_role_repository_requires_tenant_context(db_session: AsyncSession) -> None:
    user_roles = UserRoleRepository(db_session)
    user_id, role_id = uuid4(), uuid4()

    with pytest.raises(MissingTenantScopeError):
        await user_roles.assign(user_id, role_id)
    with pytest.raises(MissingTenantScopeError):
        await user_roles.revoke(user_id, role_id)
    with pytest.raises(MissingTenantScopeError):
        await user_roles.list_roles_for_user(user_id)
    with pytest.raises(MissingTenantScopeError):
        await user_roles.user_has_role(user_id, "admin")


async def test_assign_is_idempotent(
    db_session: AsyncSession, create_db_tenant: TenantFactory, create_db_user: UserFactory
) -> None:
    roles = RoleRepository(db_session)
    user_roles = UserRoleRepository(db_session)
    tenant_id = await create_db_tenant()
    update_request_context(tenant_id=tenant_id)
    role = await roles.add(Role(tenant_id=tenant_id, name="editor"))
    user_id = await create_db_user(tenant_id)

    first = await user_roles.assign(user_id, role.id)
    second = await user_roles.assign(user_id, role.id)

    assert (first, second) == (True, False)
    result = await user_roles.list_roles_for_user(user_id)
    assert len(result) == 1


async def test_list_roles_for_user_excludes_other_tenants_and_soft_deleted(
    db_session: AsyncSession,
    create_db_tenant: TenantFactory,
    create_db_user: UserFactory,
) -> None:
    roles = RoleRepository(db_session)
    user_roles = UserRoleRepository(db_session)
    tenant_id = await create_db_tenant()
    update_request_context(tenant_id=tenant_id)
    user_id = await create_db_user(tenant_id)

    kept_role = await roles.add(Role(tenant_id=tenant_id, name="kept"))
    deleted_role = await roles.add(Role(tenant_id=tenant_id, name="deleted-later"))
    await user_roles.assign(user_id, kept_role.id)
    await user_roles.assign(user_id, deleted_role.id)
    await roles.delete(deleted_role.id)

    result = await user_roles.list_roles_for_user(user_id)
    assert [r.name for r in result] == ["kept"]


async def test_user_has_role_true_and_false(
    db_session: AsyncSession, create_db_tenant: TenantFactory, create_db_user: UserFactory
) -> None:
    roles = RoleRepository(db_session)
    user_roles = UserRoleRepository(db_session)
    tenant_id = await create_db_tenant()
    update_request_context(tenant_id=tenant_id)
    role = await roles.add(Role(tenant_id=tenant_id, name="admin"))
    user_id = await create_db_user(tenant_id)
    await user_roles.assign(user_id, role.id)

    assert await user_roles.user_has_role(user_id, "admin") is True
    assert await user_roles.user_has_role(user_id, "viewer") is False


async def test_revoke_removes_assignment(
    db_session: AsyncSession, create_db_tenant: TenantFactory, create_db_user: UserFactory
) -> None:
    roles = RoleRepository(db_session)
    user_roles = UserRoleRepository(db_session)
    tenant_id = await create_db_tenant()
    update_request_context(tenant_id=tenant_id)
    role = await roles.add(Role(tenant_id=tenant_id, name="editor"))
    user_id = await create_db_user(tenant_id)
    await user_roles.assign(user_id, role.id)

    await user_roles.revoke(user_id, role.id)

    assert await user_roles.list_roles_for_user(user_id) == []


async def test_revoke_reports_whether_an_assignment_was_removed(
    db_session: AsyncSession, create_db_tenant: TenantFactory, create_db_user: UserFactory
) -> None:
    roles = RoleRepository(db_session)
    user_roles = UserRoleRepository(db_session)
    tenant_id = await create_db_tenant()
    update_request_context(tenant_id=tenant_id)
    role = await roles.add(Role(tenant_id=tenant_id, name="editor"))
    user_id = await create_db_user(tenant_id)
    await user_roles.assign(user_id, role.id)

    assert await user_roles.revoke(user_id, role.id) is True
    assert await user_roles.revoke(user_id, role.id) is False


async def test_revoke_removes_assignment_to_soft_deleted_role(
    db_session: AsyncSession, create_db_tenant: TenantFactory, create_db_user: UserFactory
) -> None:
    roles = RoleRepository(db_session)
    user_roles = UserRoleRepository(db_session)
    tenant_id = await create_db_tenant()
    update_request_context(tenant_id=tenant_id)
    role = await roles.add(Role(tenant_id=tenant_id, name="doomed"))
    user_id = await create_db_user(tenant_id)
    await user_roles.assign(user_id, role.id)
    await roles.delete(role.id)

    assert await user_roles.revoke(user_id, role.id) is True

    remaining = await db_session.execute(
        select(UserRoleModel).where(UserRoleModel.user_id == user_id)
    )
    assert remaining.scalars().all() == []


async def test_assign_with_is_default_and_paginate_reports_it(
    db_session: AsyncSession, create_db_tenant: TenantFactory, create_db_user: UserFactory
) -> None:
    roles = RoleRepository(db_session)
    user_roles = UserRoleRepository(db_session)
    tenant_id = await create_db_tenant()
    update_request_context(tenant_id=tenant_id)
    admin = await roles.add(Role(tenant_id=tenant_id, name="admin"))
    viewer = await roles.add(Role(tenant_id=tenant_id, name="viewer"))
    user_id = await create_db_user(tenant_id)
    await user_roles.assign(user_id, admin.id, is_default=True)
    await user_roles.assign(user_id, viewer.id)

    page = await user_roles.paginate_roles_for_user(user_id, OffsetParams())

    assert page.items == [
        UserRoleAssignment(role=admin, is_default=True),
        UserRoleAssignment(role=viewer, is_default=False),
    ]


async def test_get_default_role_for_user_none_when_unset(
    db_session: AsyncSession, create_db_tenant: TenantFactory, create_db_user: UserFactory
) -> None:
    roles = RoleRepository(db_session)
    user_roles = UserRoleRepository(db_session)
    tenant_id = await create_db_tenant()
    update_request_context(tenant_id=tenant_id)
    role = await roles.add(Role(tenant_id=tenant_id, name="editor"))
    user_id = await create_db_user(tenant_id)
    await user_roles.assign(user_id, role.id)

    assert await user_roles.get_default_role_for_user(user_id) is None


async def test_set_default_role_moves_the_flag_and_enforces_at_most_one(
    db_session: AsyncSession, create_db_tenant: TenantFactory, create_db_user: UserFactory
) -> None:
    roles = RoleRepository(db_session)
    user_roles = UserRoleRepository(db_session)
    tenant_id = await create_db_tenant()
    update_request_context(tenant_id=tenant_id)
    admin = await roles.add(Role(tenant_id=tenant_id, name="admin"))
    viewer = await roles.add(Role(tenant_id=tenant_id, name="viewer"))
    user_id = await create_db_user(tenant_id)
    await user_roles.assign(user_id, admin.id, is_default=True)
    await user_roles.assign(user_id, viewer.id)

    changed = await user_roles.set_default_role(user_id, viewer.id)

    assert changed is True
    assert await user_roles.get_default_role_for_user(user_id) == viewer
    rows = (
        (
            await db_session.execute(
                select(UserRoleModel).where(
                    UserRoleModel.user_id == user_id, UserRoleModel.is_default.is_(True)
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


async def test_set_default_role_unassigned_returns_false_and_changes_nothing(
    db_session: AsyncSession, create_db_tenant: TenantFactory, create_db_user: UserFactory
) -> None:
    roles = RoleRepository(db_session)
    user_roles = UserRoleRepository(db_session)
    tenant_id = await create_db_tenant()
    update_request_context(tenant_id=tenant_id)
    admin = await roles.add(Role(tenant_id=tenant_id, name="admin"))
    never_assigned = await roles.add(Role(tenant_id=tenant_id, name="never-assigned"))
    user_id = await create_db_user(tenant_id)
    await user_roles.assign(user_id, admin.id, is_default=True)

    changed = await user_roles.set_default_role(user_id, never_assigned.id)

    assert changed is False
    assert await user_roles.get_default_role_for_user(user_id) == admin


async def test_role_name_is_unique_among_live_rows(
    db_session: AsyncSession, create_db_tenant: TenantFactory
) -> None:
    roles = RoleRepository(db_session)
    tenant_id = await create_db_tenant()
    update_request_context(tenant_id=tenant_id)
    await roles.add(Role(tenant_id=tenant_id, name="editor"))

    # The DB constraint is what enforces this (the use case's pre-check can race); the
    # repository turns the violation into the domain error, not a raw IntegrityError.
    with pytest.raises(RoleAlreadyExistsError) as raised:
        await roles.add(Role(tenant_id=tenant_id, name="editor"))
    assert raised.value.context == {"name": "editor"}


async def test_role_name_is_reusable_after_soft_delete(
    db_session: AsyncSession, create_db_tenant: TenantFactory
) -> None:
    roles = RoleRepository(db_session)
    tenant_id = await create_db_tenant()
    update_request_context(tenant_id=tenant_id)
    first = await roles.add(Role(tenant_id=tenant_id, name="editor"))

    await roles.delete(first.id)
    second = await roles.add(Role(tenant_id=tenant_id, name="editor"))

    assert second.id != first.id
    assert (await roles.get_by_name("editor")) == second


async def test_role_save_preserves_created_by_and_updated_by(
    db_session: AsyncSession, create_db_tenant: TenantFactory
) -> None:
    roles = RoleRepository(db_session)
    tenant_id = await create_db_tenant()
    actor_id = uuid4()
    update_request_context(tenant_id=tenant_id)
    role = await roles.add(
        Role(tenant_id=tenant_id, name="editor", created_by=actor_id, updated_by=actor_id)
    )

    role.description = "changed"
    saved = await roles.save(role)

    assert saved.description == "changed"
    assert (saved.created_by, saved.updated_by) == (actor_id, actor_id)
