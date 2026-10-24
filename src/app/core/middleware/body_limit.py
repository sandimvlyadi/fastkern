"""Body size guard for one route, applied before FastAPI parses the request.

FastAPI reads a form body before any dependency or handler runs, so a size check inside the route
comes too late: the whole multipart body has already been spooled to disk. This ASGI middleware
refuses an oversized body while it is still being received, with or without a Content-Length.
"""

from collections.abc import Awaitable, Callable, MutableMapping
from contextlib import suppress
from typing import Any

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]


class _BodyTooLargeError(Exception):
    pass


class MaxRequestBodyMiddleware:
    def __init__(self, app: Any, *, method: str, path: str, max_bytes: int) -> None:
        self._app = app
        self._method = method
        self._path = path
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] != self._method
            or scope["path"] != self._path
        ):
            await self._app(scope, receive, send)
            return

        declared = _declared_length(scope)
        if declared is not None and declared > self._max_bytes:
            await _reject(send)
            return

        received = 0
        exceeded = False

        async def limited_receive() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._max_bytes:
                    exceeded = True
                    raise _BodyTooLargeError()
            return message

        async def guarded_send(message: Message) -> None:
            # Once the body is refused, whatever the application reports (for example the generic
            # 500 handler) is dropped; the 413 below is the only response.
            if not exceeded:
                await send(message)

        with suppress(_BodyTooLargeError):
            await self._app(scope, limited_receive, guarded_send)
        if exceeded:
            await _reject(send)


def _declared_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


async def _reject(send: Send) -> None:
    body = (
        b'{"error":{"code":"payload_too_large",'
        b'"message":"The request body is larger than allowed.",'
        b'"details":null},"meta":{"request_id":null,"pagination":null}}'
    )
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
