import pytest

from app.core.pagination import OffsetParams
from app.modules.posts.application.dto import CreatePostInput, UpdatePostInput
from app.modules.posts.application.use_cases import (
    CreatePostUseCase,
    DeletePostUseCase,
    GetPostUseCase,
    ListPostsUseCase,
    UpdatePostUseCase,
)
from app.modules.posts.domain.entities import Post
from app.modules.posts.domain.exceptions import PostNotFoundError
from tests.fakes import InMemoryPostsApiAdapter


async def test_get_post_raises_not_found_for_unknown_id() -> None:
    posts = InMemoryPostsApiAdapter()

    with pytest.raises(PostNotFoundError):
        await GetPostUseCase(posts).execute(999)


async def test_create_then_get_round_trips() -> None:
    posts = InMemoryPostsApiAdapter()

    created = await CreatePostUseCase(posts).execute(
        CreatePostInput(user_id=1, title="Hi", body="World")
    )
    fetched = await GetPostUseCase(posts).execute(created.id)

    assert fetched == created
    assert fetched.title == "Hi"


async def test_update_post_raises_not_found_for_unknown_id() -> None:
    posts = InMemoryPostsApiAdapter()

    with pytest.raises(PostNotFoundError):
        await UpdatePostUseCase(posts).execute(999, UpdatePostInput(title="x"))


async def test_update_post_returns_before_and_after() -> None:
    posts = InMemoryPostsApiAdapter(seed=[Post(id=1, user_id=1, title="Old", body="Body")])

    before, after = await UpdatePostUseCase(posts).execute(1, UpdatePostInput(title="New"))

    assert before.title == "Old"
    assert after.title == "New"
    assert after.body == "Body"


async def test_update_post_raises_not_found_when_the_post_vanishes_mid_request() -> None:
    """A race between the existence-check `get_post` and the write-endpoint `update_post`
    (e.g. another caller deletes it in between) — the use case re-checks rather than trusting
    the external API's write-endpoint status code alone."""

    class _VanishesOnWrite(InMemoryPostsApiAdapter):
        async def update_post(
            self, post_id: int, *, title: str | None, body: str | None
        ) -> Post | None:
            return None

    posts = _VanishesOnWrite(seed=[Post(id=1, user_id=1, title="Old", body="Body")])

    with pytest.raises(PostNotFoundError):
        await UpdatePostUseCase(posts).execute(1, UpdatePostInput(title="New"))


async def test_delete_post_raises_not_found_for_unknown_id() -> None:
    posts = InMemoryPostsApiAdapter()

    with pytest.raises(PostNotFoundError):
        await DeletePostUseCase(posts).execute(999)


async def test_delete_post_removes_it() -> None:
    posts = InMemoryPostsApiAdapter(seed=[Post(id=1, user_id=1, title="Old", body="Body")])

    before = await DeletePostUseCase(posts).execute(1)

    assert before.id == 1
    assert await posts.get_post(1) is None


async def test_list_posts_paginates() -> None:
    posts = InMemoryPostsApiAdapter(
        seed=[Post(id=i, user_id=1, title=f"t{i}", body="b") for i in range(1, 6)]
    )

    page = await ListPostsUseCase(posts).execute(OffsetParams(page=1, page_size=2))

    assert page.total == 5
    assert len(page.items) == 2
