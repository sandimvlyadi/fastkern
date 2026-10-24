"""Baseline HTTP security headers on every response.

Pure ASGI (not `BaseHTTPMiddleware`): it only touches the response-start message, so it
needs no per-request task or body buffering.

Deliberately NOT set: a Content-Security-Policy. `/docs` and `/redoc` load Swagger UI / ReDoc
from a CDN, and a policy strict enough to mean something would break them. If a CSP is wanted
later, scope it to `/api/*` only.
"""

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import Settings

_API_PATH_PREFIX = "/api/"
# One year; includeSubDomains because a deployment serving the API over HTTPS on a host serves
# its siblings the same way. No `preload`: that is an irreversible opt-in for the operator.
_HSTS = "max-age=31536000; includeSubDomains"


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, *, settings: Settings) -> None:
        self._app = app
        self._hsts = settings.is_production

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        is_api = scope["path"].startswith(_API_PATH_PREFIX)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Referrer-Policy", "no-referrer")
                if is_api:
                    # Tokens and per-user data: nothing here belongs in a shared/browser cache.
                    headers.setdefault("Cache-Control", "no-store")
                if self._hsts:
                    # Only meaningful over HTTPS, and a stale HSTS on a dev host is a nuisance.
                    headers.setdefault("Strict-Transport-Security", _HSTS)
            await send(message)

        await self._app(scope, receive, send_with_headers)
