from abc import ABC, abstractmethod
from uuid import UUID

from app.core.pagination import OffsetPage, OffsetParams
from app.modules.master_examples.domain.entities import MasterExample


class MasterExampleRepositoryPort(ABC):
    @abstractmethod
    async def get_by_id(self, example_id: UUID) -> MasterExample | None: ...

    @abstractmethod
    async def get_by_name(
        self, name: str, *, tenant_id: UUID | None = None
    ) -> MasterExample | None: ...

    @abstractmethod
    async def paginate(self, params: OffsetParams) -> OffsetPage[MasterExample]: ...

    @abstractmethod
    async def add(self, example: MasterExample) -> MasterExample: ...

    @abstractmethod
    async def save(self, example: MasterExample) -> MasterExample: ...

    @abstractmethod
    async def delete(self, example_id: UUID) -> None: ...
