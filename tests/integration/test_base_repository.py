from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import update_request_context
from app.core.pagination import OffsetParams
from app.modules.audit_log.domain.entities import AuditLogEntry
from app.modules.audit_log.infrastructure.repository import AuditLogRepository
from app.modules.users.domain.entities import User
from app.modules.users.infrastructure.repository import UserRepository
from app.shared.infrastructure.repository import BaseRepository, MissingTenantScopeError
from tests.helpers import TenantFactory


def _user(tenant_id: UUID) -> User:
    return User(
        tenant_id=tenant_id,
        email=f"{uuid4()}@example.com",
        hashed_password="h",
        full_name="Test User",
    )


async def test_get_by_id_requires_tenant_scope(
    db_session: AsyncSession, create_db_tenant: TenantFactory
) -> None:
    users = UserRepository(db_session)
    user = await users.add(_user(await create_db_tenant()))
    with pytest.raises(MissingTenantScopeError):
        await users.get_by_id(user.id)


async def test_get_by_id_explicit_tenant_override_works_without_context(
    db_session: AsyncSession,
    create_db_tenant: TenantFactory,
) -> None:
    users = UserRepository(db_session)
    user = await users.add(_user(await create_db_tenant()))
    found = await users.get_by_id(user.id, tenant_id=user.tenant_id)
    assert found is not None
    assert found.id == user.id


async def test_get_by_id_tenant_scoped_false_sees_other_tenants(
    db_session: AsyncSession, create_db_tenant: TenantFactory
) -> None:
    users = UserRepository(db_session)
    user = await users.add(_user(await create_db_tenant()))
    update_request_context(tenant_id=uuid4())  # a *different* tenant than the user's
    found = await users.get_by_id(user.id, tenant_scoped=False)
    assert found is not None
    assert found.id == user.id


async def test_paginate_requires_tenant_scope(db_session: AsyncSession) -> None:
    users = UserRepository(db_session)
    with pytest.raises(MissingTenantScopeError):
        await users.paginate(OffsetParams())


async def test_get_by_email_wrong_tenant_finds_nothing(
    db_session: AsyncSession, create_db_tenant: TenantFactory
) -> None:
    users = UserRepository(db_session)
    user = await users.add(_user(await create_db_tenant()))
    # get_by_email always takes an explicit tenant_id, so it never raises
    # MissingTenantScopeError -- but a *wrong* tenant_id must find nothing.
    assert await users.get_by_email(user.email, tenant_id=uuid4()) is None
    assert await users.get_by_email(user.email, tenant_id=user.tenant_id) is not None


async def test_save_on_missing_row_raises_lookup_error(db_session: AsyncSession) -> None:
    users = UserRepository(db_session)
    ghost = _user(uuid4())  # never persisted, so no real tenant needed
    update_request_context(tenant_id=ghost.tenant_id)
    with pytest.raises(LookupError):
        await users.save(ghost)


async def test_save_requires_tenant_scope(
    db_session: AsyncSession, create_db_tenant: TenantFactory
) -> None:
    """save() is routed through _base_query() like every other method —
    a caller with no resolvable tenant can't silently write past scoping,
    the same way get_by_id()/paginate() already refuse to."""
    users = UserRepository(db_session)
    user = await users.add(_user(await create_db_tenant()))
    with pytest.raises(MissingTenantScopeError):
        await users.save(user)


async def test_save_keeps_created_at_and_refreshes_updated_at(
    db_session: AsyncSession, create_db_tenant: TenantFactory
) -> None:
    users = UserRepository(db_session)
    user = await users.add(_user(await create_db_tenant()))
    update_request_context(tenant_id=user.tenant_id)
    original_created_at = user.created_at

    user.full_name = "Renamed"
    saved = await users.save(user)

    assert saved.created_at == original_created_at
    assert saved.full_name == "Renamed"


async def test_save_explicit_tenant_override_works_without_context(
    db_session: AsyncSession,
    create_db_tenant: TenantFactory,
) -> None:
    users = UserRepository(db_session)
    user = await users.add(_user(await create_db_tenant()))
    user.full_name = "Renamed"
    saved = await users.save(user, tenant_id=user.tenant_id)
    assert saved.full_name == "Renamed"


async def test_save_cannot_overwrite_another_tenants_row(
    db_session: AsyncSession, create_db_tenant: TenantFactory
) -> None:
    """Regression test for the bypass: save() must not resolve a row
    outside the caller's tenant scope, even though the mutated entity
    carries the target row's own id."""
    users = UserRepository(db_session)
    victim = await users.add(_user(await create_db_tenant()))
    forged = User(
        id=victim.id,
        tenant_id=victim.tenant_id,
        email=victim.email,
        hashed_password=victim.hashed_password,
        full_name="Attacker-Controlled Name",
    )
    update_request_context(tenant_id=uuid4())  # a *different* tenant than victim's
    with pytest.raises(LookupError):
        await users.save(forged)


async def test_delete_missing_id_is_noop(db_session: AsyncSession) -> None:
    users = UserRepository(db_session)
    update_request_context(tenant_id=uuid4())
    await users.delete(uuid4())  # must not raise


async def test_soft_delete_hides_row_unless_include_deleted(
    db_session: AsyncSession, create_db_tenant: TenantFactory
) -> None:
    users = UserRepository(db_session)
    user = await users.add(_user(await create_db_tenant()))
    update_request_context(tenant_id=user.tenant_id)

    await users.delete(user.id)

    assert await users.get_by_id(user.id) is None
    still_there = await users.get_by_id(user.id, include_deleted=True)
    assert still_there is not None
    assert still_there.deleted_at is not None


async def test_hard_delete_removes_row(
    db_session: AsyncSession, create_db_tenant: TenantFactory
) -> None:
    users = UserRepository(db_session)
    user = await users.add(_user(await create_db_tenant()))
    update_request_context(tenant_id=user.tenant_id)

    await users.delete(user.id, hard=True)

    assert await users.get_by_id(user.id, include_deleted=True) is None


async def test_delete_on_model_without_soft_delete_hard_deletes(db_session: AsyncSession) -> None:
    audit_logs = AuditLogRepository(db_session)
    entry = await audit_logs.record(
        AuditLogEntry(
            tenant_id=uuid4(), actor_id=None, action="x", resource_type="y", resource_id=None
        )
    )
    update_request_context(tenant_id=entry.tenant_id)

    await audit_logs.delete(entry.id)

    page = await audit_logs.paginate(OffsetParams())
    assert all(e.id != entry.id for e in page.items)


async def test_paginate_totals_and_include_deleted(
    db_session: AsyncSession, create_db_tenant: TenantFactory
) -> None:
    users = UserRepository(db_session)
    tenant_id = await create_db_tenant()
    update_request_context(tenant_id=tenant_id)
    for _ in range(3):
        await users.add(_user(tenant_id))

    page = await users.paginate(OffsetParams(page=1, page_size=20))
    assert page.total == 3
    assert page.page == 1
    assert page.page_size == 20

    first_user = page.items[0]
    await users.delete(first_user.id)

    active_page = await users.paginate(OffsetParams())
    assert active_page.total == 2

    # UserRepository narrows the inherited `paginate()` signature (its port
    # doesn't expose `include_deleted`), so exercise the base implementation
    # directly to cover that branch.
    all_page = await BaseRepository.paginate(users, OffsetParams(), include_deleted=True)
    assert all_page.total == 3


async def test_add_stamps_created_by_and_updated_by_from_request_context(
    db_session: AsyncSession, create_db_tenant: TenantFactory
) -> None:
    tenant_id = await create_db_tenant()
    actor_id = uuid4()
    update_request_context(tenant_id=tenant_id, user_id=actor_id)
    created = await UserRepository(db_session).add(_user(tenant_id))
    assert (created.created_by, created.updated_by) == (actor_id, actor_id)
    assert created.deleted_by is None


async def test_add_without_authenticated_user_leaves_actors_null(
    db_session: AsyncSession, create_db_tenant: TenantFactory
) -> None:
    created = await UserRepository(db_session).add(_user(await create_db_tenant()))
    assert (created.created_by, created.updated_by) == (None, None)


async def test_add_keeps_an_actor_already_set_on_the_entity(
    db_session: AsyncSession, create_db_tenant: TenantFactory
) -> None:
    tenant_id = await create_db_tenant()
    imported_by = uuid4()
    update_request_context(tenant_id=tenant_id, user_id=uuid4())
    user = _user(tenant_id)
    user.created_by = imported_by
    created = await UserRepository(db_session).add(user)
    assert created.created_by == imported_by


async def test_save_stamps_updated_by_but_never_rewrites_created_by(
    db_session: AsyncSession, create_db_tenant: TenantFactory
) -> None:
    tenant_id = await create_db_tenant()
    creator, editor = uuid4(), uuid4()
    users = UserRepository(db_session)
    update_request_context(tenant_id=tenant_id, user_id=creator)
    user = await users.add(_user(tenant_id))

    update_request_context(user_id=editor)
    user.full_name = "Renamed"
    saved = await users.save(user)
    assert (saved.created_by, saved.updated_by) == (creator, editor)


async def test_save_without_authenticated_user_keeps_previous_updated_by(
    db_session: AsyncSession, create_db_tenant: TenantFactory
) -> None:
    """A Celery task / seed edit has no actor: it must leave updated_by alone,
    not null it out."""
    tenant_id = await create_db_tenant()
    creator = uuid4()
    users = UserRepository(db_session)
    update_request_context(tenant_id=tenant_id, user_id=creator)
    user = await users.add(_user(tenant_id))

    update_request_context(user_id=None)
    user.full_name = "Renamed by a job"
    saved = await users.save(user, tenant_id=tenant_id)
    assert saved.updated_by == creator


async def test_delete_records_deleted_by_and_moves_updated_by_with_it(
    db_session: AsyncSession, create_db_tenant: TenantFactory
) -> None:
    """A soft delete is an UPDATE (the trigger bumps updated_at), so updated_by
    must name the deleter too, alongside deleted_by."""
    tenant_id = await create_db_tenant()
    creator, remover = uuid4(), uuid4()
    users = UserRepository(db_session)
    update_request_context(tenant_id=tenant_id, user_id=creator)
    user = await users.add(_user(tenant_id))

    update_request_context(user_id=remover)
    await users.delete(user.id)
    gone = await users.get_by_id(user.id, include_deleted=True)
    assert gone is not None
    assert gone.deleted_at is not None
    assert (gone.deleted_by, gone.updated_by, gone.created_by) == (remover, remover, creator)
