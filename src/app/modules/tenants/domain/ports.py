from abc import ABC, abstractmethod
from uuid import UUID

from app.core.pagination import OffsetPage, OffsetParams
from app.modules.tenants.domain.entities import Tenant


class TenantRepositoryPort(ABC):
    @abstractmethod
    async def get_by_id(self, tenant_id: UUID) -> Tenant | None: ...

    @abstractmethod
    async def get_by_slug(self, slug: str) -> Tenant | None: ...

    @abstractmethod
    async def list_all(self) -> list[Tenant]: ...

    @abstractmethod
    async def paginate(self, params: OffsetParams) -> OffsetPage[Tenant]: ...

    @abstractmethod
    async def add(self, tenant: Tenant) -> Tenant: ...

    @abstractmethod
    async def save(self, tenant: Tenant) -> Tenant: ...

    @abstractmethod
    async def delete(self, tenant_id: UUID) -> None:
        """Soft-deletes the tenant."""
        ...


class TenantRoleProvisionerPort(ABC):
    """What tenant creation needs from the roles module, without importing it: wired to the
    roles module's own use case in `app.core.di` (the composition root)."""

    @abstractmethod
    async def provision_baseline_roles(self, tenant_id: UUID) -> None:
        """Gives a just-created tenant its baseline system roles (`admin`, `user`), so its
        first users have something to be assigned and self-registration can hand out `user`."""
        ...
