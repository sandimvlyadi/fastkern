"""Use cases for the `posts` module — one class per use case."""

from app.core.pagination import OffsetPage, OffsetParams
from app.modules.posts.application.dto import CreatePostInput, UpdatePostInput
from app.modules.posts.domain.entities import Post
from app.modules.posts.domain.exceptions import PostNotFoundError
from app.modules.posts.domain.ports import PostsApiPort


class ListPostsUseCase:
    def __init__(self, posts: PostsApiPort) -> None:
        self._posts = posts

    async def execute(self, params: OffsetParams) -> OffsetPage[Post]:
        return await self._posts.list_posts(params)


class GetPostUseCase:
    def __init__(self, posts: PostsApiPort) -> None:
        self._posts = posts

    async def execute(self, post_id: int) -> Post:
        post = await self._posts.get_post(post_id)
        if post is None:
            raise PostNotFoundError(post_id=str(post_id))
        return post


class CreatePostUseCase:
    def __init__(self, posts: PostsApiPort) -> None:
        self._posts = posts

    async def execute(self, data: CreatePostInput) -> Post:
        return await self._posts.create_post(user_id=data.user_id, title=data.title, body=data.body)


class UpdatePostUseCase:
    def __init__(self, posts: PostsApiPort) -> None:
        self._posts = posts

    async def execute(self, post_id: int, data: UpdatePostInput) -> tuple[Post, Post]:
        """Returns (before, after). Fetches first so an unknown id 404s here rather than
        relying on the external API's write-endpoint status code (see adapter's
        `update_post`/`delete_post` docstrings)."""
        before = await self._posts.get_post(post_id)
        if before is None:
            raise PostNotFoundError(post_id=str(post_id))
        after = await self._posts.update_post(post_id, title=data.title, body=data.body)
        if after is None:
            raise PostNotFoundError(post_id=str(post_id))
        return before, after


class DeletePostUseCase:
    def __init__(self, posts: PostsApiPort) -> None:
        self._posts = posts

    async def execute(self, post_id: int) -> Post:
        before = await self._posts.get_post(post_id)
        if before is None:
            raise PostNotFoundError(post_id=str(post_id))
        await self._posts.delete_post(post_id)
        return before
