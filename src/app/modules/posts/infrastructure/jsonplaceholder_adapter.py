"""Adapter implementing `PostsApiPort` against the external JSONPlaceholder `/posts` API.

Mirrors `app.modules.notifications.infrastructure.email_adapter`: tenacity retries transient
failures (timeouts, 5xx, 429); a 404 is mapped to the specific domain "not found" result
instead of being retried. Every other failure — an unexpected status, a body that isn't the
expected JSON — becomes `PostsServiceError`, never an unhandled 500.

Only idempotent calls (GET/PATCH/DELETE) are retried on any transient failure. A POST that
reached the server may already have created the post even if the reply never arrived, so it is
retried only when the request provably was not sent (connection failures) or was refused with
a 429; a retried POST could otherwise create the same post twice.

Known limitation: JSONPlaceholder (a `json-server` fake REST API) does not actually persist
writes — a `POST`/`PATCH`/`DELETE` is echoed back and then forgotten, so a created post will
404 on a later `GET`. This is a property of the demo backend, not a bug in this adapter.
"""

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.logging import get_logger
from app.core.pagination import OffsetPage, OffsetParams
from app.modules.posts.domain.entities import Post
from app.modules.posts.domain.exceptions import PostsServiceError
from app.modules.posts.domain.ports import PostsApiPort

logger = get_logger(__name__)

# Failures that happen before the request leaves this process, so retrying cannot repeat a
# side effect on the server.
_NOT_SENT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


class _TransientPostsApiError(Exception):
    """Raised for retryable failures only."""


def _to_post(data: Any) -> Post:
    try:
        return Post(id=data["id"], user_id=data["userId"], title=data["title"], body=data["body"])
    except (KeyError, TypeError) as exc:
        logger.error("posts_api_unexpected_payload")
        raise PostsServiceError() from exc


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as exc:  # json.JSONDecodeError and UnicodeDecodeError are both ValueErrors
        logger.error("posts_api_unexpected_payload", status_code=response.status_code)
        raise PostsServiceError() from exc


class JsonPlaceholderPostsAdapter(PostsApiPort):
    def __init__(
        self,
        *,
        base_url: str,
        auth_header: str,
        auth_token: str,
        timeout_seconds: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._auth_header = auth_header
        self._auth_token = auth_token
        self._timeout_seconds = timeout_seconds
        self._injected_client = client

    @contextlib.asynccontextmanager
    async def _client(self) -> AsyncIterator[httpx.AsyncClient]:
        """The injected client (left open: its owner closes it), else a short-lived one. Not
        cached on the instance: this adapter is constructed fresh per request by the DI
        provider, and (as with the email adapter) a cached connection pool would outlive the
        event loop it was opened on in a worker/task context."""
        if self._injected_client is not None:
            yield self._injected_client
            return
        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            yield client

    def _headers(self) -> dict[str, str]:
        return {self._auth_header: f"Bearer {self._auth_token}"}

    @retry(
        retry=retry_if_exception_type(_TransientPostsApiError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
        reraise=True,
    )
    async def _request(
        self,
        method: str,
        path: str,
        *,
        idempotent: bool,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        try:
            async with self._client() as client:
                response = await client.request(
                    method,
                    f"{self._base_url}{path}",
                    params=params,
                    json=json,
                    headers=self._headers(),
                )
        except httpx.TransportError as exc:
            if idempotent or isinstance(exc, _NOT_SENT_ERRORS):
                raise _TransientPostsApiError() from exc
            raise  # not retried: the server may already have acted on it
        if response.status_code == 429 or (idempotent and response.status_code >= 500):
            raise _TransientPostsApiError()
        return response

    async def _send(
        self,
        method: str,
        path: str,
        *,
        idempotent: bool = True,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """A request with retries. Returns a success or 404 response; any other outcome is
        logged and raised as `PostsServiceError`."""
        try:
            response = await self._request(
                method, path, idempotent=idempotent, params=params, json=json
            )
        except (_TransientPostsApiError, httpx.TransportError) as exc:
            logger.error("posts_api_request_failed_after_retries", method=method, path=path)
            raise PostsServiceError() from exc
        if response.is_error and response.status_code != 404:
            logger.error(
                "posts_api_unexpected_status",
                method=method,
                path=path,
                status_code=response.status_code,
            )
            raise PostsServiceError(status_code=response.status_code)
        return response

    async def list_posts(self, params: OffsetParams) -> OffsetPage[Post]:
        response = await self._send(
            "GET", "/posts", params={"_page": params.page, "_limit": params.page_size}
        )
        if response.status_code == 404:
            raise PostsServiceError(status_code=404)  # the collection itself never 404s
        rows = _json(response)
        if not isinstance(rows, list):
            logger.error("posts_api_unexpected_payload", path="/posts")
            raise PostsServiceError()
        # If a future JSONPlaceholder response ever omits this header, this degrades to
        # total=0 rather than crashing — acceptable for this demo module.
        total = int(response.headers.get("X-Total-Count", "0"))
        items = [_to_post(row) for row in rows]
        return OffsetPage(items=items, total=total, page=params.page, page_size=params.page_size)

    async def get_post(self, post_id: int) -> Post | None:
        response = await self._send("GET", f"/posts/{post_id}")
        if response.status_code == 404:
            return None
        return _to_post(_json(response))

    async def create_post(self, *, user_id: int, title: str, body: str) -> Post:
        response = await self._send(
            "POST",
            "/posts",
            idempotent=False,
            json={"userId": user_id, "title": title, "body": body},
        )
        if response.status_code == 404:
            raise PostsServiceError(status_code=404)
        return _to_post(_json(response))

    async def update_post(
        self, post_id: int, *, title: str | None, body: str | None
    ) -> Post | None:
        payload = {k: v for k, v in {"title": title, "body": body}.items() if v is not None}
        response = await self._send("PATCH", f"/posts/{post_id}", json=payload)
        if response.status_code == 404:
            return None
        return _to_post(_json(response))

    async def delete_post(self, post_id: int) -> None:
        # json-server returns 200+{} for both a successful delete and (per the API's own
        # quirk) sometimes for an already-gone id; callers that need a strict 404 do a
        # get_post() check first (see UpdatePostUseCase/DeletePostUseCase) rather than relying
        # on this status code alone. So a 404 here is tolerated, not an error.
        await self._send("DELETE", f"/posts/{post_id}")
