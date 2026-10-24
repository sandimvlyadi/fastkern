from collections.abc import Callable

import httpx
import pytest
import tenacity

from app.core.pagination import OffsetParams
from app.modules.posts.domain.exceptions import PostsServiceError
from app.modules.posts.infrastructure.jsonplaceholder_adapter import JsonPlaceholderPostsAdapter


@pytest.fixture(autouse=True)
def _no_retry_backoff() -> None:
    """Retries in these tests are triggered on purpose; skip the real exponential backoff so
    they don't add seconds to the suite."""
    JsonPlaceholderPostsAdapter._request.retry.wait = tenacity.wait_none()


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _adapter(handler: Callable[[httpx.Request], httpx.Response]) -> JsonPlaceholderPostsAdapter:
    return JsonPlaceholderPostsAdapter(
        base_url="https://provider.test",
        auth_header="Authorization",
        auth_token="secret-token",
        client=_client(handler),
    )


async def test_list_posts_parses_total_count_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["_page"] == "2"
        assert request.url.params["_limit"] == "10"
        return httpx.Response(
            200,
            headers={"X-Total-Count": "100"},
            json=[{"id": 11, "userId": 1, "title": "t", "body": "b"}],
        )

    page = await _adapter(handler).list_posts(OffsetParams(page=2, page_size=10))

    assert page.total == 100
    assert page.page == 2
    assert page.page_size == 10
    assert page.items[0].id == 11


async def test_get_post_returns_none_on_404() -> None:
    adapter = _adapter(lambda request: httpx.Response(404))

    assert await adapter.get_post(999) is None


async def test_get_post_retries_then_raises_after_exhaustion() -> None:
    attempts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        return httpx.Response(500)

    with pytest.raises(PostsServiceError):
        await _adapter(handler).get_post(1)

    assert len(attempts) == 3


async def test_every_request_carries_the_configured_auth_header() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": 1, "userId": 1, "title": "t", "body": "b"})

    await _adapter(handler).get_post(1)

    assert requests[0].headers["authorization"] == "Bearer secret-token"


async def test_create_post_sends_expected_payload() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={"id": 101, "userId": 1, "title": "new", "body": "b"})

    post = await _adapter(handler).create_post(user_id=1, title="new", body="b")

    assert post.id == 101
    assert requests[0].method == "POST"


async def test_update_post_returns_none_on_404() -> None:
    adapter = _adapter(lambda request: httpx.Response(404))

    assert await adapter.update_post(999, title="x", body=None) is None


async def test_delete_post_raises_on_unexpected_error_status() -> None:
    adapter = _adapter(lambda request: httpx.Response(400))

    with pytest.raises(PostsServiceError):
        await adapter.delete_post(1)


async def test_list_posts_retries_then_raises_after_exhaustion() -> None:
    adapter = _adapter(lambda request: httpx.Response(500))

    with pytest.raises(PostsServiceError):
        await adapter.list_posts(OffsetParams(page=1, page_size=10))


async def test_create_post_retries_then_raises_after_exhaustion() -> None:
    adapter = _adapter(lambda request: httpx.Response(500))

    with pytest.raises(PostsServiceError):
        await adapter.create_post(user_id=1, title="t", body="b")


async def test_update_post_returns_the_patched_post_on_success() -> None:
    adapter = _adapter(
        lambda request: httpx.Response(
            200, json={"id": 1, "userId": 1, "title": "patched", "body": "b"}
        )
    )

    post = await adapter.update_post(1, title="patched", body=None)

    assert post is not None
    assert post.title == "patched"


async def test_update_post_retries_then_raises_after_exhaustion() -> None:
    adapter = _adapter(lambda request: httpx.Response(500))

    with pytest.raises(PostsServiceError):
        await adapter.update_post(1, title="x", body=None)


async def test_delete_post_retries_then_raises_after_exhaustion() -> None:
    adapter = _adapter(lambda request: httpx.Response(500))

    with pytest.raises(PostsServiceError):
        await adapter.delete_post(1)


async def test_a_transport_failure_is_retried_and_eventually_raises() -> None:
    """Exercises the real (non-mocked-transport) `_client()` branch and the
    `httpx.TransportError` handling in `_request`: nothing listens on this port, so every
    attempt fails to connect rather than returning any HTTP response at all."""
    adapter = JsonPlaceholderPostsAdapter(
        base_url="http://127.0.0.1:1",
        auth_header="Authorization",
        auth_token="secret-token",
        timeout_seconds=1.0,
    )

    with pytest.raises(PostsServiceError):
        await adapter.get_post(1)


@pytest.mark.parametrize("status_code", [400, 401, 403, 422])
async def test_an_unexpected_client_error_status_is_a_service_error_not_a_crash(
    status_code: int,
) -> None:
    """Only a 404 has a domain meaning; any other 4xx body must not reach `_to_post`."""
    attempts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        return httpx.Response(status_code, json={"error": "nope"})

    adapter = _adapter(handler)
    with pytest.raises(PostsServiceError):
        await adapter.get_post(1)
    with pytest.raises(PostsServiceError):
        await adapter.list_posts(OffsetParams())
    assert len(attempts) == 2  # a 4xx is not transient: no retries


async def test_a_body_that_is_not_json_is_a_service_error() -> None:
    adapter = _adapter(lambda request: httpx.Response(200, text="<html>maintenance</html>"))

    with pytest.raises(PostsServiceError):
        await adapter.get_post(1)


async def test_a_post_missing_fields_is_a_service_error() -> None:
    adapter = _adapter(lambda request: httpx.Response(200, json={"id": 1}))

    with pytest.raises(PostsServiceError):
        await adapter.get_post(1)


async def test_a_list_body_that_is_not_a_list_is_a_service_error() -> None:
    adapter = _adapter(lambda request: httpx.Response(200, json={"items": []}))

    with pytest.raises(PostsServiceError):
        await adapter.list_posts(OffsetParams())


async def test_a_404_on_the_collection_is_a_service_error() -> None:
    adapter = _adapter(lambda request: httpx.Response(404))

    with pytest.raises(PostsServiceError):
        await adapter.list_posts(OffsetParams())
    with pytest.raises(PostsServiceError):
        await adapter.create_post(user_id=1, title="t", body="b")


async def test_create_post_is_not_retried_on_a_5xx() -> None:
    """The server may have created the post before failing; retrying could create it twice."""
    attempts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        return httpx.Response(502)

    with pytest.raises(PostsServiceError):
        await _adapter(handler).create_post(user_id=1, title="t", body="b")
    assert len(attempts) == 1


async def test_create_post_is_not_retried_when_the_reply_times_out() -> None:
    attempts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        raise httpx.ReadTimeout("no reply", request=request)

    with pytest.raises(PostsServiceError):
        await _adapter(handler).create_post(user_id=1, title="t", body="b")
    assert len(attempts) == 1


def _refuse_connection(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("refused", request=request)


@pytest.mark.parametrize(
    "failure",
    [_refuse_connection, lambda request: httpx.Response(429)],
    ids=["connect-error", "429"],
)
async def test_create_post_is_retried_when_the_request_was_not_processed(
    failure: Callable[[httpx.Request], httpx.Response],
) -> None:
    attempts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        if len(attempts) == 1:
            return failure(request)
        return httpx.Response(201, json={"id": 101, "userId": 1, "title": "t", "body": "b"})

    post = await _adapter(handler).create_post(user_id=1, title="t", body="b")

    assert post.id == 101
    assert len(attempts) == 2


async def test_update_post_is_retried_when_the_reply_times_out() -> None:
    """PATCH is idempotent, so any transient failure is retried."""
    attempts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        if len(attempts) == 1:
            raise httpx.ReadTimeout("no reply", request=request)
        return httpx.Response(200, json={"id": 1, "userId": 1, "title": "x", "body": "b"})

    post = await _adapter(handler).update_post(1, title="x", body=None)

    assert post is not None
    assert len(attempts) == 2
