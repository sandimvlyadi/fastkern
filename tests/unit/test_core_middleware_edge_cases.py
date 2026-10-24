"""Pure-ASGI edge branches of the body-limit and security-headers middleware that a normal HTTP
request through the test client never reaches: a malformed `Content-Length` header, and a
non-HTTP ASGI scope (e.g. a lifespan or websocket event)."""

from typing import Any

from app.core.config import Settings, get_settings
from app.core.middleware.body_limit import _declared_length
from app.core.middleware.security_headers import SecurityHeadersMiddleware


def test_declared_length_ignores_a_malformed_content_length_header() -> None:
    scope = {"headers": [(b"content-length", b"not-a-number")]}
    assert _declared_length(scope) is None


async def test_security_headers_passes_through_a_non_http_scope() -> None:
    called: dict[str, Any] = {}

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        called["scope_type"] = scope["type"]

    async def receive() -> dict[str, Any]:
        raise AssertionError("not called for a lifespan scope")

    async def send(message: dict[str, Any]) -> None:
        raise AssertionError("not called for a lifespan scope")

    settings: Settings = get_settings()
    middleware = SecurityHeadersMiddleware(app, settings=settings)

    await middleware({"type": "lifespan"}, receive, send)

    assert called["scope_type"] == "lifespan"
