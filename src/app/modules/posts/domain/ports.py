"""Port the application layer depends on. Infrastructure implements this
against the external JSONPlaceholder API (dependency inversion) so use
cases never import httpx directly."""

from abc import ABC, abstractmethod

from app.core.pagination import OffsetPage, OffsetParams
from app.modules.posts.domain.entities import Post


class PostsApiPort(ABC):
    @abstractmethod
    async def list_posts(self, params: OffsetParams) -> OffsetPage[Post]: ...

    @abstractmethod
    async def get_post(self, post_id: int) -> Post | None: ...

    @abstractmethod
    async def create_post(self, *, user_id: int, title: str, body: str) -> Post: ...

    @abstractmethod
    async def update_post(
        self, post_id: int, *, title: str | None, body: str | None
    ) -> Post | None: ...

    @abstractmethod
    async def delete_post(self, post_id: int) -> None: ...
