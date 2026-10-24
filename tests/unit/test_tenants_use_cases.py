from uuid import uuid4

import pytest

from app.core.pagination import OffsetParams
from app.modules.tenants.application.use_cases import (
    ActivateTenantUseCase,
    CreateTenantInput,
    CreateTenantUseCase,
    DeactivateTenantUseCase,
    DeleteTenantUseCase,
    ListTenantsUseCase,
    UpdateTenantInput,
    UpdateTenantUseCase,
)
from app.modules.tenants.domain.entities import Tenant
from app.modules.tenants.domain.exceptions import (
    CannotModifyOwnTenantError,
    TenantNotFoundError,
    TenantSlugAlreadyExistsError,
)
from tests.fakes import InMemoryTenantRepository, RecordingTenantRoleProvisioner


async def test_create_tenant_success() -> None:
    tenants = InMemoryTenantRepository()
    provisioner = RecordingTenantRoleProvisioner()
    tenant = await CreateTenantUseCase(tenants, provisioner).execute(
        CreateTenantInput(name="Acme", slug="acme")
    )
    assert tenant.name == "Acme"
    assert tenant.slug == "acme"


async def test_create_tenant_provisions_baseline_roles_for_the_new_tenant() -> None:
    tenants = InMemoryTenantRepository()
    provisioner = RecordingTenantRoleProvisioner()

    tenant = await CreateTenantUseCase(tenants, provisioner).execute(
        CreateTenantInput(name="Acme", slug="acme")
    )

    assert provisioner.provisioned_for == [tenant.id]


async def test_create_tenant_duplicate_slug_raises_and_provisions_nothing() -> None:
    tenants = InMemoryTenantRepository()
    provisioner = RecordingTenantRoleProvisioner()
    data = CreateTenantInput(name="Acme", slug="acme")
    first = await CreateTenantUseCase(tenants, provisioner).execute(data)
    with pytest.raises(TenantSlugAlreadyExistsError):
        await CreateTenantUseCase(tenants, provisioner).execute(
            CreateTenantInput(name="Acme 2", slug="acme")
        )
    assert provisioner.provisioned_for == [first.id]


async def test_list_tenants() -> None:
    tenants = InMemoryTenantRepository()
    await tenants.add(Tenant(name="A", slug="a"))
    await tenants.add(Tenant(name="B", slug="b"))
    result = await ListTenantsUseCase(tenants).execute(OffsetParams())
    assert result.total == 2
    assert len(result.items) == 2


async def test_update_tenant_changes_name_and_slug_and_returns_before_after() -> None:
    tenants = InMemoryTenantRepository()
    tenant = await tenants.add(Tenant(name="Acme", slug="acme"))

    before, after = await UpdateTenantUseCase(tenants).execute(
        tenant.id, UpdateTenantInput(name="Acme Holdings", slug="acme-holdings")
    )

    assert (before.name, before.slug) == ("Acme", "acme")
    assert (after.name, after.slug) == ("Acme Holdings", "acme-holdings")


async def test_update_tenant_does_not_touch_activation() -> None:
    tenants = InMemoryTenantRepository()
    tenant = await tenants.add(Tenant(name="Acme", slug="acme", is_active=False))

    _, after = await UpdateTenantUseCase(tenants).execute(tenant.id, UpdateTenantInput(name="X"))

    assert after.is_active is False


async def test_update_tenant_slug_taken_by_another_raises() -> None:
    tenants = InMemoryTenantRepository()
    await tenants.add(Tenant(name="A", slug="taken"))
    tenant = await tenants.add(Tenant(name="B", slug="b"))

    with pytest.raises(TenantSlugAlreadyExistsError):
        await UpdateTenantUseCase(tenants).execute(tenant.id, UpdateTenantInput(slug="taken"))


async def test_update_tenant_keeping_its_own_slug_is_not_a_conflict() -> None:
    tenants = InMemoryTenantRepository()
    tenant = await tenants.add(Tenant(name="A", slug="a"))

    _, after = await UpdateTenantUseCase(tenants).execute(
        tenant.id, UpdateTenantInput(name="A2", slug="a")
    )

    assert after.name == "A2"


async def test_update_tenant_unknown_raises() -> None:
    with pytest.raises(TenantNotFoundError):
        await UpdateTenantUseCase(InMemoryTenantRepository()).execute(uuid4(), UpdateTenantInput())


async def test_deactivate_then_activate_tenant() -> None:
    tenants = InMemoryTenantRepository()
    tenant = await tenants.add(Tenant(name="A", slug="a"))

    before, after = await DeactivateTenantUseCase(tenants).execute(
        tenant.id, caller_tenant_id=uuid4()
    )
    assert (before.is_active, after.is_active) == (True, False)

    before, after = await ActivateTenantUseCase(tenants).execute(tenant.id)
    assert (before.is_active, after.is_active) == (False, True)


async def test_deactivate_own_tenant_raises_and_changes_nothing() -> None:
    tenants = InMemoryTenantRepository()
    tenant = await tenants.add(Tenant(name="A", slug="a"))

    with pytest.raises(CannotModifyOwnTenantError):
        await DeactivateTenantUseCase(tenants).execute(tenant.id, caller_tenant_id=tenant.id)

    assert tenants.tenants[tenant.id].is_active is True


async def test_deactivate_and_activate_unknown_tenant_raise() -> None:
    tenants = InMemoryTenantRepository()
    with pytest.raises(TenantNotFoundError):
        await DeactivateTenantUseCase(tenants).execute(uuid4(), caller_tenant_id=uuid4())
    with pytest.raises(TenantNotFoundError):
        await ActivateTenantUseCase(tenants).execute(uuid4())


async def test_delete_tenant_returns_before_and_deletes() -> None:
    tenants = InMemoryTenantRepository()
    tenant = await tenants.add(Tenant(name="A", slug="a"))

    before = await DeleteTenantUseCase(tenants).execute(tenant.id, caller_tenant_id=uuid4())

    assert before.slug == "a"
    assert tenants.deleted_ids == [tenant.id]


async def test_delete_own_tenant_raises_and_deletes_nothing() -> None:
    tenants = InMemoryTenantRepository()
    tenant = await tenants.add(Tenant(name="A", slug="a"))

    with pytest.raises(CannotModifyOwnTenantError):
        await DeleteTenantUseCase(tenants).execute(tenant.id, caller_tenant_id=tenant.id)

    assert tenants.deleted_ids == []


async def test_delete_unknown_tenant_raises_and_deletes_nothing() -> None:
    tenants = InMemoryTenantRepository()
    with pytest.raises(TenantNotFoundError):
        await DeleteTenantUseCase(tenants).execute(uuid4(), caller_tenant_id=uuid4())
    assert tenants.deleted_ids == []
