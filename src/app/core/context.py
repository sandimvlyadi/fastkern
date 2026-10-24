"""Request-scoped context propagated via contextvars.

Populated by `app.core.middleware.request_context` at the start of every
request and read anywhere downstream (repositories, logging, audit log)
without threading parameters through every function signature.
"""

from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo


@dataclass(frozen=True, slots=True)
class RequestContext:
    request_id: str
    tenant_id: UUID | None
    user_id: UUID | None
    active_role: str | None
    locale: str
    timezone: ZoneInfo
    session_id: str | None = None
    """The login session (JWT `sid` claim) behind this request; None before authentication or
    for a token minted before sessions existed."""
    api_client_id: UUID | None = None
    """The authenticated ApiKey behind this request (see `get_current_api_client`); None for a
    human (`user_id`-authenticated) request. The two are mutually exclusive in practice, never
    both set on the same request."""


_request_context: ContextVar[RequestContext | None] = ContextVar("_request_context", default=None)

_DEFAULT_CONTEXT = RequestContext(
    request_id="-",
    tenant_id=None,
    user_id=None,
    active_role=None,
    locale="en",
    timezone=ZoneInfo("UTC"),
)


def get_request_context() -> RequestContext:
    return _request_context.get() or _DEFAULT_CONTEXT


def set_request_context(context: RequestContext) -> None:
    _request_context.set(context)


def reset_request_context() -> None:
    _request_context.set(None)


def update_request_context(**changes: Any) -> RequestContext:
    """Merge `changes` into the current context (e.g. after authenticating
    a request: user_id, tenant_id, active_role)."""
    new_context = replace(get_request_context(), **changes)
    set_request_context(new_context)
    return new_context
