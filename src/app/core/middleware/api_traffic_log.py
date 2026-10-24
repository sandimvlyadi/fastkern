"""Logs raw traffic for API-key-authenticated (`partners` router) requests only.

Separate from `RequestContextMiddleware`'s per-request logging: this writes a
row to `sys_api_request_logs` (via a fire-and-forget Celery task — see
`app.modules.api_clients.infrastructure.tasks`), not the structured app log,
and only for requests where `get_current_api_client` actually ran. This
table is kept separate from `sys_audit_logs` because it is high-volume
observability/billing-support data with its own short retention, not the
compliance-grade audit trail.
"""

import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from starlette.background import BackgroundTasks
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.celery_app import dispatch_task
from app.core.logging import get_logger

logger = get_logger(__name__)


async def _dispatch_request_log(**payload: Any) -> None:
    # Best effort: the partner's request has already been handled, so a broker outage must
    # cost a log row, never turn that response into a 500.
    try:
        await dispatch_task("api_clients.record_request_log", **payload)
    except Exception as exc:
        logger.warning(
            "api_request_log_dispatch_failed", error_type=type(exc).__name__, error=str(exc)
        )


class ApiTrafficLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # No response to attach the log to: the error propagates to the outermost handler.
            payload = self._payload(request, start, status_code=500, response=None)
            if payload is not None:
                await _dispatch_request_log(**payload)
            raise
        payload = self._payload(request, start, status_code=response.status_code, response=response)
        if payload is not None:
            # Enqueued as a background task, which runs only after the response body has gone
            # out: publishing to the broker must never delay the partner's response, least of
            # all while the broker is slow or down.
            tasks = BackgroundTasks()
            if response.background is not None:
                tasks.add_task(response.background)
            tasks.add_task(_dispatch_request_log, **payload)
            response.background = tasks
        return response

    @staticmethod
    def _payload(
        request: Request, start: float, *, status_code: int, response: Response | None
    ) -> dict[str, Any] | None:
        """The log row's fields, or None for a request that did not authenticate with an API key
        (a human-authenticated or pre-auth request is not partner traffic).

        Read off `request.state`, not the request-context contextvar that
        `get_current_api_client` also sets: `BaseHTTPMiddleware.call_next` runs everything from
        this middleware down to the router in a separate anyio task, so a contextvar mutation
        made there is invisible here once `call_next()` returns. `request.state` (the shared
        ASGI scope dict) is the one thing that reliably crosses that boundary."""
        api_client_id = getattr(request.state, "api_client_id", None)
        if api_client_id is None:
            return None
        content_length = response.headers.get("content-length") if response is not None else None
        return {
            "tenant_id": str(getattr(request.state, "api_client_tenant_id", None)),
            "api_key_id": str(api_client_id),
            "method": request.method,
            "path": request.url.path,
            "status_code": status_code,
            "latency_ms": round((time.perf_counter() - start) * 1000),
            "response_size_bytes": int(content_length) if content_length is not None else None,
            # Also becomes the key's `last_used_at`, which is written off the request path.
            "used_at": datetime.now(UTC).isoformat(),
        }
