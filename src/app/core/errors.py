"""Central exception -> HTTP response mapping.

This is the ONLY place that translates `AppError` subclasses (and a few
framework exceptions) into HTTP status codes. Presentation-layer routers
should never catch these — let them bubble up here.
"""

import socket
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from kombu import exceptions as kombu_exc
from redis import exceptions as redis_exc
from sqlalchemy import exc as sa_exc
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.context import get_request_context
from app.core.exceptions import (
    AlreadyExistsError,
    AppError,
    ConflictError,
    ExternalServiceError,
    ForbiddenError,
    GoneError,
    NotFoundError,
    PayloadTooLargeError,
    RateLimitedError,
    ServiceUnavailableError,
    UnauthorizedError,
    ValidationAppError,
)
from app.core.i18n import translate
from app.core.logging import get_logger
from app.core.middleware.request_context import REQUEST_ID_HEADER
from app.core.response import ErrorEnvelope, error_envelope

logger = get_logger(__name__)

_STATUS_BY_ERROR: dict[type[AppError], int] = {
    NotFoundError: status.HTTP_404_NOT_FOUND,
    AlreadyExistsError: status.HTTP_409_CONFLICT,
    ConflictError: status.HTTP_409_CONFLICT,
    GoneError: status.HTTP_410_GONE,
    PayloadTooLargeError: status.HTTP_413_CONTENT_TOO_LARGE,
    ValidationAppError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    UnauthorizedError: status.HTTP_401_UNAUTHORIZED,
    ForbiddenError: status.HTTP_403_FORBIDDEN,
    RateLimitedError: status.HTTP_429_TOO_MANY_REQUESTS,
    ExternalServiceError: status.HTTP_502_BAD_GATEWAY,
    ServiceUnavailableError: status.HTTP_503_SERVICE_UNAVAILABLE,
}

# Failures to *reach* a dependency (database, Redis), as opposed to it rejecting a statement.
# A dead Postgres does not surface as a SQLAlchemy `DBAPIError`: asyncpg raises the socket-level
# error itself (ConnectionRefusedError, socket.gaierror, ...), hence the builtins. These mean
# "retry shortly" (503), never "bug" (500). Deliberately NOT here: DataError/IntegrityError/
# ProgrammingError (a bad statement is a bug) and a bare OSError (FileNotFoundError etc. is too).
_DEPENDENCY_UNREACHABLE: tuple[type[Exception], ...] = (
    ConnectionError,
    socket.gaierror,
    TimeoutError,
    sa_exc.TimeoutError,  # connection pool exhausted
    redis_exc.ConnectionError,
    redis_exc.TimeoutError,
    # Celery can't reach its broker (Redis) to enqueue a task. Not a `ConnectionError` subclass.
    kombu_exc.OperationalError,
)

# Fake-but-plausible request id used only in generated OpenAPI examples
# below (see `error_responses()`) — never a real request's id.
_EXAMPLE_REQUEST_ID = "3c7a1e2b-0f4d-4a6b-8c2e-1a2b3c4d5e6f"


def _validation_error_response() -> dict[str, Any]:
    # A new dict per call: a caller may mutate its own `responses=` dict.
    return {
        "model": ErrorEnvelope,
        "description": "Validation Error",
        "content": {
            "application/json": {
                "example": {
                    "error": {
                        "code": "validation_error",
                        "message": "Request validation failed.",
                        "details": {
                            "errors": [
                                {
                                    "type": "missing",
                                    "loc": ["body", "email"],
                                    "msg": "Field required",
                                }
                            ]
                        },
                    },
                    "meta": {"request_id": _EXAMPLE_REQUEST_ID, "pagination": None},
                }
            }
        },
    }


def _safe_validation_errors(exc: RequestValidationError) -> list[dict[str, Any]]:
    """`type`/`loc`/`msg` only. Pydantic's own `input` (and `ctx`) would echo what the client
    sent — passwords included — back in the body (and into proxy/APM logs), and for a
    non-JSON body `input` is raw `bytes`, which is not even JSON-serializable."""
    return [
        {
            "type": str(error.get("type", "")),
            "loc": jsonable_encoder(list(error.get("loc", ()))),
            "msg": str(error.get("msg", "")),
        }
        for error in exc.errors()
    ]


def _status_for_class(error_cls: type[AppError]) -> int:
    for error_type, http_status in _STATUS_BY_ERROR.items():
        if issubclass(error_cls, error_type):
            return http_status
    return status.HTTP_500_INTERNAL_SERVER_ERROR


def _status_for(error: AppError) -> int:
    return _status_for_class(type(error))


def error_responses(
    *exc_classes: type[AppError], validation: bool = False
) -> dict[int | str, dict[str, Any]]:
    """Builds an OpenAPI `responses=` dict with realistic, per-exception
    examples grouped under their real HTTP status code — so `/docs` and
    `/redoc` show the actual `{"error": {...}, "meta": {...}}` envelope
    instead of FastAPI's generic placeholder.

    Pass the concrete `AppError` subclasses a given endpoint's dependencies
    and use case(s) can actually raise (traced from the code, not
    guessed) — see each router's call site. `validation=True` documents
    the endpoint's 422 with the real envelope shape too, overriding
    FastAPI's default `HTTPValidationError` doc (only added when "422"
    isn't already a key in `responses`).

    Raises `ValueError` at call time (i.e. at router-definition/import
    time, not silently at request time) if two of the passed classes share
    both a status code and a `code` — that pair would otherwise silently
    overwrite one example with the other in the generated docs.
    """
    grouped: dict[int, dict[str, type[AppError]]] = {}
    for cls in exc_classes:
        by_code = grouped.setdefault(_status_for_class(cls), {})
        if cls.code in by_code and by_code[cls.code] is not cls:
            raise ValueError(
                f"error_responses(): {cls.code!r} is used by both "
                f"{by_code[cls.code].__name__} and {cls.__name__} under the "
                "same HTTP status — give one of them a distinct `code`."
            )
        by_code[cls.code] = cls

    responses: dict[int | str, dict[str, Any]] = {}
    for http_status, by_code in grouped.items():
        responses[http_status] = {
            "model": ErrorEnvelope,
            "content": {
                "application/json": {
                    "examples": {
                        code: {
                            "summary": cls.message,
                            "value": {
                                "error": {
                                    "code": cls.code,
                                    "message": cls.message,
                                    "details": None,
                                },
                                "meta": {"request_id": _EXAMPLE_REQUEST_ID, "pagination": None},
                            },
                        }
                        for code, cls in by_code.items()
                    }
                }
            },
        }

    if validation:
        responses[status.HTTP_422_UNPROCESSABLE_CONTENT] = _validation_error_response()

    return responses


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        ctx = get_request_context()
        http_status = _status_for(exc)
        if http_status >= 500 and not isinstance(exc, ServiceUnavailableError):
            logger.exception("unhandled_app_error", code=exc.code, error=str(exc))
        return JSONResponse(
            status_code=http_status,
            content=error_envelope(
                code=exc.code,
                message=translate(exc.key, ctx.locale, default=exc.message, **exc.context),
                request_id=ctx.request_id,
                details=exc.context or None,
            ),
            headers=(
                exc.headers if isinstance(exc, RateLimitedError | ServiceUnavailableError) else None
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        ctx = get_request_context()
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=error_envelope(
                code="validation_error",
                message=translate(
                    "validation.request_failed", ctx.locale, default="Request validation failed."
                ),
                request_id=ctx.request_id,
                details={"errors": _safe_validation_errors(exc)},
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # `detail` is free text (or Starlette's own "Not Found"), not a catalog key, so it is
        # sent untranslated. Raise an AppError subclass to get a translated message.
        return JSONResponse(
            status_code=exc.status_code,
            content=error_envelope(
                code="http_error",
                message=str(exc.detail),
                request_id=get_request_context().request_id,
            ),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        # This handler is run by `ServerErrorMiddleware`, outside `RequestContextMiddleware`: the
        # contextvar has already been reset and the middleware's response header never gets
        # added. Read the id/locale the middleware left on `request.state` instead, and set the
        # header here, so a 500 can still be correlated with the logs.
        ctx = get_request_context()
        request_id = getattr(request.state, "request_id", None) or ctx.request_id
        locale = getattr(request.state, "locale", None) or ctx.locale
        logger.exception("unhandled_exception", error=str(exc), request_id=request_id)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=error_envelope(
                code="internal_error",
                message=translate(
                    "error.internal", locale, default="An unexpected error occurred."
                ),
                request_id=request_id,
            ),
            headers={REQUEST_ID_HEADER: request_id},
        )

    async def handle_dependency_unreachable(request: Request, exc: Exception) -> JSONResponse:
        # A warning, not `logger.exception`: during an outage every request lands here, and a
        # traceback per request would bury the one line that matters.
        logger.warning("dependency_unreachable", error_type=type(exc).__name__, error=str(exc))
        return await handle_app_error(request, ServiceUnavailableError())

    for error_type in _DEPENDENCY_UNREACHABLE:
        app.add_exception_handler(error_type, handle_dependency_unreachable)

    @app.exception_handler(sa_exc.DBAPIError)
    async def handle_database_error(request: Request, exc: sa_exc.DBAPIError) -> JSONResponse:
        # Only connection-level failures are "try again"; DataError/IntegrityError/
        # ProgrammingError and friends are bugs and keep the plain 500.
        if isinstance(exc, sa_exc.OperationalError | sa_exc.InterfaceError) or (
            exc.connection_invalidated
        ):
            return await handle_dependency_unreachable(request, exc)
        return await handle_unexpected_error(request, exc)
