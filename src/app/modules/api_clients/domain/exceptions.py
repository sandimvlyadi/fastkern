from app.core.exceptions import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
    ValidationAppError,
)


class ApiClientNotFoundError(NotFoundError):
    code = "api_client_not_found"
    key = "api_client.not_found"
    message = "API client not found."


class ApiKeyAlreadyRevokedError(ConflictError):
    """Raised when an admin tries to rotate a key that is no longer active. A 409, not a 401:
    the admin is authenticated fine, but the target key's state conflicts with the requested
    action."""

    code = "api_key_already_revoked"
    key = "api_client.key_already_revoked"
    message = "This API key has already been revoked and cannot be rotated."


class ApiKeyAlreadyExpiredError(ConflictError):
    """Raised when an admin tries to rotate a key past its `expires_at`: rotation keeps the
    expiry, so the new plaintext would be refused on its very first use."""

    code = "api_key_already_expired"
    key = "api_client.key_already_expired"
    message = "This API key has expired and cannot be rotated."


class InvalidApiKeyError(UnauthorizedError):
    code = "invalid_api_key"
    key = "api_client.invalid_key"
    message = "The API key is invalid."


class ApiKeyExpiredError(UnauthorizedError):
    code = "api_key_expired"
    key = "api_client.key_expired"
    message = "This API key has expired."


class ScopeDeniedError(ForbiddenError):
    code = "scope_denied"
    key = "api_client.scope_denied"
    message = "This API key does not have the '{scope}' scope."


class UnknownScopeError(ValidationAppError):
    """An API key may only be given scopes from `api_clients.domain.scopes.API_KEY_SCOPE_CATALOG`
    — the scopes some partner route actually checks with `require_scope(...)`."""

    code = "unknown_scope"
    key = "api_client.unknown_scope"
    message = "These scopes do not exist."


class ScopeEscalationError(ForbiddenError):
    """A caller may only give an API key scopes they hold as RBAC permissions of the same name.
    Otherwise `api_clients:write` alone would mint a key that reads what its creator cannot."""

    code = "scope_escalation"
    key = "api_client.scope_escalation"
    message = "You cannot grant scopes you do not hold as permissions."
