from dataclasses import dataclass, replace
from uuid import UUID

from app.core.pagination import OffsetPage, OffsetParams
from app.modules.tenants.domain.entities import Tenant
from app.modules.tenants.domain.exceptions import (
    CannotModifyOwnTenantError,
    TenantNotFoundError,
    TenantSlugAlreadyExistsError,
)
from app.modules.tenants.domain.ports import TenantRepositoryPort, TenantRoleProvisionerPort


@dataclass(frozen=True, slots=True)
class CreateTenantInput:
    name: str
    slug: str


@dataclass(frozen=True, slots=True)
class UpdateTenantInput:
    """Fields left as None are unchanged. Activation state is not editable
    here — it goes through the activate/deactivate use cases."""

    name: str | None = None
    slug: str | None = None


class CreateTenantUseCase:
    def __init__(
        self, tenants: TenantRepositoryPort, role_provisioner: TenantRoleProvisionerPort
    ) -> None:
        self._tenants = tenants
        self._role_provisioner = role_provisioner

    async def execute(self, data: CreateTenantInput) -> Tenant:
        if await self._tenants.get_by_slug(data.slug) is not None:
            raise TenantSlugAlreadyExistsError(slug=data.slug)
        tenant = await self._tenants.add(Tenant(name=data.name, slug=data.slug))
        # Same request, same DB transaction as the tenant itself: a tenant never exists
        # without its baseline roles. (Bootstrapping its first admin is a platform-superadmin
        # step: `POST /roles/users/{id}` — see docs/module-guide.md.)
        await self._role_provisioner.provision_baseline_roles(tenant.id)
        return tenant


class ListTenantsUseCase:
    def __init__(self, tenants: TenantRepositoryPort) -> None:
        self._tenants = tenants

    async def execute(self, params: OffsetParams) -> OffsetPage[Tenant]:
        return await self._tenants.paginate(params)


class UpdateTenantUseCase:
    def __init__(self, tenants: TenantRepositoryPort) -> None:
        self._tenants = tenants

    async def execute(self, tenant_id: UUID, data: UpdateTenantInput) -> tuple[Tenant, Tenant]:
        """Returns (before, after) for the caller's audit-log diff."""
        tenant = await self._tenants.get_by_id(tenant_id)
        if tenant is None:
            raise TenantNotFoundError(tenant_id=str(tenant_id))
        before = replace(tenant)
        if data.slug is not None and data.slug != tenant.slug:
            existing = await self._tenants.get_by_slug(data.slug)
            if existing is not None and existing.id != tenant.id:
                raise TenantSlugAlreadyExistsError(slug=data.slug)
            tenant.slug = data.slug
        if data.name is not None:
            tenant.name = data.name
        after = await self._tenants.save(tenant)
        return before, after


class DeactivateTenantUseCase:
    def __init__(self, tenants: TenantRepositoryPort) -> None:
        self._tenants = tenants

    async def execute(self, tenant_id: UUID, *, caller_tenant_id: UUID) -> tuple[Tenant, Tenant]:
        """Suspends a tenant (reversible): its users can no longer log in or
        refresh. Refuses the caller's own tenant — that would lock them out."""
        if tenant_id == caller_tenant_id:
            raise CannotModifyOwnTenantError(tenant_id=str(tenant_id))
        tenant = await self._tenants.get_by_id(tenant_id)
        if tenant is None:
            raise TenantNotFoundError(tenant_id=str(tenant_id))
        before = replace(tenant)
        tenant.deactivate()
        after = await self._tenants.save(tenant)
        return before, after


class ActivateTenantUseCase:
    def __init__(self, tenants: TenantRepositoryPort) -> None:
        self._tenants = tenants

    async def execute(self, tenant_id: UUID) -> tuple[Tenant, Tenant]:
        tenant = await self._tenants.get_by_id(tenant_id)
        if tenant is None:
            raise TenantNotFoundError(tenant_id=str(tenant_id))
        before = replace(tenant)
        tenant.activate()
        after = await self._tenants.save(tenant)
        return before, after


class DeleteTenantUseCase:
    def __init__(self, tenants: TenantRepositoryPort) -> None:
        self._tenants = tenants

    async def execute(self, tenant_id: UUID, *, caller_tenant_id: UUID) -> Tenant:
        """Soft-deletes a tenant and returns it as it was, for the audit log. There is no
        restore endpoint; refuses the caller's own tenant."""
        if tenant_id == caller_tenant_id:
            raise CannotModifyOwnTenantError(tenant_id=str(tenant_id))
        tenant = await self._tenants.get_by_id(tenant_id)
        if tenant is None:
            raise TenantNotFoundError(tenant_id=str(tenant_id))
        before = replace(tenant)
        await self._tenants.delete(tenant_id)
        return before
