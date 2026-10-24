"""Standard API response envelope used by every endpoint.

Success:
    {"data": ..., "meta": {...} | null}
Error (see errors.py for the exception -> envelope mapping):
    {"error": {"code": "...", "message": "...", "details": {...} | null}}

Deliberately plain dicts, not Pydantic response models: every router
builds its payload from a module-specific schema (see each module's
`presentation/schemas.py`) and wraps it with these two functions, so
there is exactly one envelope-shape definition to keep in sync.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PaginationMeta(BaseModel):
    """Mirrors the dict shape `OffsetPage.to_meta()` builds by hand — kept
    as a separate typed model (not imported from `core.pagination`) purely
    so `response_model` can document it; the two must stay in sync."""

    model_config = ConfigDict(populate_by_name=True)

    page: int
    page_size: int = Field(alias="pageSize")
    total: int
    total_pages: int = Field(alias="totalPages")


class ResponseMeta(BaseModel):
    request_id: str
    pagination: PaginationMeta | None = None


class SuccessEnvelope[T](BaseModel):
    """`response_model` shape for the `success_envelope()` dict below — for
    OpenAPI documentation only; routers keep returning plain dicts built by
    `success_envelope()`, this just lets FastAPI describe their real shape
    in `/docs` and `/redoc` instead of an untyped object."""

    data: T
    meta: ResponseMeta


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, Any] | None = None


class ErrorEnvelope(BaseModel):
    """`responses=` shape for the `error_envelope()` dict below — see
    `app.core.errors.error_responses()`, which uses this as the `model` for
    each documented error status code, for OpenAPI documentation only."""

    error: ErrorDetail
    meta: ResponseMeta


def success_envelope(
    data: Any, *, request_id: str, pagination: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "data": data,
        "meta": {"request_id": request_id, "pagination": pagination},
    }


def error_envelope(
    *, code: str, message: str, request_id: str, details: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "error": {"code": code, "message": message, "details": details},
        "meta": {"request_id": request_id, "pagination": None},
    }
