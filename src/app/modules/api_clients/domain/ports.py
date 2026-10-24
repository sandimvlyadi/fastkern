"""Ports the application layer depends on. Infrastructure implements these
(dependency inversion) so use cases never import SQLAlchemy directly."""

from abc import ABC, abstractmethod
from uuid import UUID

from app.core.pagination import OffsetPage, OffsetParams
from app.modules.api_clients.domain.entities import ApiKey


class ApiKeyRepositoryPort(ABC):
    @abstractmethod
    async def get_by_id(self, api_key_id: UUID, *, tenant_scoped: bool = True) -> ApiKey | None: ...

    @abstractmethod
    async def get_by_key_hash(self, key_hash: str) -> ApiKey | None:
        """Matches either the current or still-in-overlap `previous_key_hash`.
        Deliberately NOT tenant-scoped: the caller's tenant isn't known yet at
        this point — this is the same bootstrap problem `get_current_user`
        solves by calling `users.get_by_id(..., tenant_scoped=False)`."""
        ...

    @abstractmethod
    async def add(self, api_key: ApiKey) -> ApiKey: ...

    @abstractmethod
    async def save(self, api_key: ApiKey) -> ApiKey: ...

    @abstractmethod
    async def paginate(self, params: OffsetParams) -> OffsetPage[ApiKey]: ...
