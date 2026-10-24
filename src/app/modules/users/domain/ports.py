"""Ports the application layer depends on. Infrastructure implements these
(dependency inversion) so use cases never import SQLAlchemy directly."""

from abc import ABC, abstractmethod
from uuid import UUID

from app.core.pagination import OffsetPage, OffsetParams
from app.modules.users.domain.entities import User


class UserRepositoryPort(ABC):
    @abstractmethod
    async def get_by_id(self, user_id: UUID, *, tenant_scoped: bool = True) -> User | None: ...

    @abstractmethod
    async def get_by_email(self, email: str, *, tenant_id: UUID) -> User | None:
        """Email is only unique per tenant (a unique index on `(tenant_id, email)`), so
        tenant_id is required here rather than read from request context: this is called
        from register/login, before any request context exists."""
        ...

    @abstractmethod
    async def add(self, user: User) -> User: ...

    @abstractmethod
    async def save(self, user: User) -> User: ...

    @abstractmethod
    async def delete(self, user_id: UUID) -> None: ...

    @abstractmethod
    async def paginate(self, params: OffsetParams) -> OffsetPage[User]: ...
