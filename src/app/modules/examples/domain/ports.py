from abc import ABC, abstractmethod
from uuid import UUID

from app.core.pagination import OffsetPage, OffsetParams
from app.modules.examples.domain.entities import Example


class ExampleRepositoryPort(ABC):
    @abstractmethod
    async def get_by_id(self, example_id: UUID) -> Example | None: ...

    @abstractmethod
    async def paginate(self, params: OffsetParams) -> OffsetPage[Example]: ...

    @abstractmethod
    async def add(self, example: Example) -> Example: ...

    @abstractmethod
    async def save(self, example: Example) -> Example: ...

    @abstractmethod
    async def delete(self, example_id: UUID) -> None: ...
