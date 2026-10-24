"""Domain-level exception hierarchy.

These carry no HTTP knowledge — `app.core.errors` maps them to HTTP
responses centrally. Raise these (or module-specific subclasses of them)
from domain/application code; never raise `HTTPException` outside the
presentation layer.
"""

from typing import Any


class AppError(Exception):
    """Base class for all application-raised errors.

    `key` is a translation lookup for `app.core.i18n.translate()` — the
    *only* place that calls `translate()` for these is the central handler
    in `app.core.errors`, using it as `translate(exc.key, locale,
    default=exc.message, **exc.context)`. Every subclass MUST set its own
    `key` (with a matching entry in `app/locales/en.json` **and**
    `id.json`). This is not optional: `tests/unit/test_i18n_key_coverage.py`
    fails CI if a subclass is missing a `key` or a locale file is missing
    an entry for one.
    """

    code: str = "internal_error"
    key: str = "error.internal"
    message: str = "An unexpected error occurred."

    def __init__(self, message: str | None = None, **context: Any) -> None:
        self.message = message or self.message
        self.context = context
        super().__init__(self.message)


class NotFoundError(AppError):
    code = "not_found"
    key = "error.not_found"
    message = "The requested resource was not found."


class AlreadyExistsError(AppError):
    code = "already_exists"
    key = "error.already_exists"
    message = "The resource already exists."


class ValidationAppError(AppError):
    code = "validation_error"
    key = "error.validation"
    message = "Invalid input."


class UnauthorizedError(AppError):
    code = "unauthorized"
    key = "error.unauthorized"
    message = "Authentication is required."


class ForbiddenError(AppError):
    code = "forbidden"
    key = "error.forbidden"
    message = "You do not have permission to perform this action."


class ConflictError(AppError):
    code = "conflict"
    key = "error.conflict"
    message = "The request conflicts with the current state of the resource."


class RateLimitedError(AppError):
    code = "rate_limited"
    key = "error.rate_limited"
    message = "Too many requests."

    def __init__(
        self,
        message: str | None = None,
        *,
        headers: dict[str, str] | None = None,
        **context: Any,
    ) -> None:
        super().__init__(message, **context)
        # Response headers (`Retry-After`, `X-RateLimit-*`) for the central
        # handler in `app.core.errors` to attach — this class stays free of
        # any HTTP-framework import.
        self.headers = headers or {}


class ServiceUnavailableError(AppError):
    """A dependency this service needs (database, Redis) is unreachable right now. Mapped to
    503 + `Retry-After`: the request was fine and retrying shortly is the right client move,
    unlike a 500 (a bug) or a 502 (an upstream answered badly)."""

    code = "service_unavailable"
    key = "error.service_unavailable"
    message = "The service is temporarily unavailable. Please try again shortly."

    def __init__(
        self, message: str | None = None, *, retry_after_seconds: int = 5, **context: Any
    ) -> None:
        super().__init__(message, **context)
        # Response headers for the central handler in `app.core.errors` to attach.
        self.headers = {"Retry-After": str(retry_after_seconds)}


class GoneError(AppError):
    """The resource existed but is no longer usable (e.g. an expired upload session). 410."""

    code = "gone"
    key = "error.gone"
    message = "The resource is no longer available."


class PayloadTooLargeError(AppError):
    code = "payload_too_large"
    key = "error.payload_too_large"
    message = "The request body is larger than allowed."


class ExternalServiceError(AppError):
    code = "external_service_error"
    key = "error.external_service"
    message = "An upstream service failed."
