from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from pydantic import AfterValidator, AwareDatetime, ConfigDict, Field

from app.modules.api_clients.domain.scopes import MAX_SCOPES_PER_KEY, is_valid_scope
from app.shared.application.dto import CamelModel

_EXAMPLE_ID = "5e2a1f3b-7c6d-4e8a-9f1b-2d3c4e5f6a7b"
_EXAMPLE_TENANT_ID = "3fa85f64-5717-4562-b3fc-2c963f66afa6"


def _validate_scopes(values: list[str]) -> list[str]:
    if len(values) > MAX_SCOPES_PER_KEY:
        raise ValueError(f"an API key can hold at most {MAX_SCOPES_PER_KEY} scopes")
    if not all(is_valid_scope(value) for value in values):
        # Not echoed back: the offending strings are client input.
        raise ValueError(
            "each scope must be 'resource:action' (lowercase letters, digits and underscores)"
        )
    return list(dict.fromkeys(values))  # drop duplicates, keep the caller's order


Scopes = Annotated[list[str], AfterValidator(_validate_scopes)]


def _validate_expiry(value: datetime | None) -> datetime | None:
    # A past expiry would create a key that is refused on its first use.
    if value is not None and value <= datetime.now(UTC):
        raise ValueError("expiresAt must be in the future")
    return value


# Timezone-aware only: a naive value would be read in whatever timezone the database session
# happens to use.
ExpiresAt = Annotated[AwareDatetime | None, AfterValidator(_validate_expiry)]


class ApiKeyScopeResponse(CamelModel):
    """One entry of `GET /api-clients/scopes` — what an API key's `scopes` may contain, and
    whether the caller may grant it (they must hold the permission of the same name)."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"code": "posts:read", "resource": "posts", "action": "read", "grantable": True}
            ]
        }
    )

    code: str
    resource: str
    action: str
    grantable: bool


class CreateApiKeyRequest(CamelModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [{"name": "Acme Corp integration", "scopes": ["posts:read"]}]
        }
    )

    name: str = Field(min_length=1, max_length=200)
    scopes: Scopes = Field(default_factory=list)
    expires_at: ExpiresAt = None


class ApiKeyResponse(CamelModel):
    """Never includes the plaintext key or any hash — see `ApiKeyCreatedResponse` for the one
    response that does, exactly once, at creation/rotation time."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": _EXAMPLE_ID,
                    "tenantId": _EXAMPLE_TENANT_ID,
                    "name": "Acme Corp integration",
                    "keyPrefix": "pak_AbCdEfGh",
                    "scopes": ["posts:read"],
                    "status": "active",
                    "expiresAt": None,
                    "lastUsedAt": "2026-03-02T10:12:45Z",
                    "createdAt": "2026-03-01T09:00:00Z",
                }
            ]
        }
    )

    id: UUID
    tenant_id: UUID
    name: str
    key_prefix: str
    scopes: list[str]
    status: str
    expires_at: datetime | None
    last_used_at: datetime | None
    created_at: datetime


class ApiKeyCreatedResponse(ApiKeyResponse):
    """Returned only by create/rotate, the one time the plaintext key is shown. A separate
    schema so no other endpoint can return the secret by reusing it."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": _EXAMPLE_ID,
                    "tenantId": _EXAMPLE_TENANT_ID,
                    "name": "Acme Corp integration",
                    "keyPrefix": "pak_AbCdEfGh",
                    "scopes": ["posts:read"],
                    "status": "active",
                    "expiresAt": None,
                    "lastUsedAt": None,
                    "createdAt": "2026-03-01T09:00:00Z",
                    "plaintextKey": "<api-key-returned-once-by-this-endpoint-store-it-now>",
                }
            ]
        }
    )

    plaintext_key: str
