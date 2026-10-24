"""Populates the per-request context: request id, tenant, locale, timezone.

Runs first in the middleware stack so every downstream layer (logging,
repositories, audit log) can rely on `get_request_context()` being set.
Authentication adds `user_id` / `active_role` later, in the `get_current_user` dependency
(`app.core.di`), once the JWT is verified.
"""

import time
import uuid
from collections.abc import Awaitable, Callable
from zoneinfo import ZoneInfo

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import Settings
from app.core.context import RequestContext, reset_request_context, set_request_context
from app.core.i18n import negotiate_locale
from app.core.logging import get_logger
from app.core.timezone import parse_timezone

logger = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
TIMEZONE_HEADER = "X-Timezone"
ACCEPT_LANGUAGE_HEADER = "Accept-Language"


class RequestContextMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: object, *, settings: Settings) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._settings = settings

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())

        locale = negotiate_locale(
            request.headers.get(ACCEPT_LANGUAGE_HEADER),
            self._settings.supported_locales,
            self._settings.default_locale,
        )

        timezone = self._settings.default_timezone
        if tz_header := request.headers.get(TIMEZONE_HEADER):
            timezone = tz_header
        # An untrusted header: any unusable value falls back to the default instead of failing
        # the request (see `parse_timezone` for why ZoneInfoNotFoundError alone is not enough).
        tzinfo = parse_timezone(timezone) or ZoneInfo(self._settings.default_timezone)

        context = RequestContext(
            request_id=request_id,
            tenant_id=None,
            user_id=None,
            active_role=None,
            locale=locale,
            timezone=tzinfo,
        )
        set_request_context(context)
        # `ServerErrorMiddleware` (which renders unhandled 500s) sits OUTSIDE this middleware, so it
        # only runs after the contextvar below is reset; the ASGI scope's state survives.
        request.state.request_id = request_id
        request.state.locale = locale

        start = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            duration_ms = (time.perf_counter() - start) * 1000
            logger.info(
                "request_completed",
                method=request.method,
                path=request.url.path,
                duration_ms=round(duration_ms, 2),
            )
            reset_request_context()

        response.headers[REQUEST_ID_HEADER] = request_id
        return response
