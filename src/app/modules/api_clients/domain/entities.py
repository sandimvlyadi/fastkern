"""Domain entities for the `api_clients` module. Plain dataclasses only —
no FastAPI/SQLAlchemy imports (enforced by import-linter).
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from app.shared.domain.entity import TenantScopedEntity

ApiKeyStatus = Literal["active", "revoked"]


def _aware(value: datetime) -> datetime:
    """SQLite drops tzinfo on round-trip even for a `DateTime(timezone=True)` column
    (Postgres does not); every comparison against `datetime.now(UTC)` must normalize
    first, same as `files.domain.entities.UploadSession.is_expired`."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@dataclass(kw_only=True)
class ApiKey(TenantScopedEntity):
    """A partner's long-lived credential. Structurally separate from `User` on
    purpose — it has no password, no session, no RBAC role, and is
    issued/managed by a human admin, never by itself."""

    name: str
    key_prefix: str
    key_hash: str
    scopes: list[str] = field(default_factory=list)
    status: ApiKeyStatus = "active"
    previous_key_hash: str | None = None
    previous_key_expires_at: datetime | None = None
    expires_at: datetime | None = None
    last_used_at: datetime | None = None

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes or "*" in self.scopes

    def is_usable(self, *, now: datetime) -> bool:
        if self.status != "active":
            return False
        return not self.is_expired(now=now)

    def is_expired(self, *, now: datetime) -> bool:
        return self.expires_at is not None and _aware(self.expires_at) <= now

    def previous_key_usable(self, *, now: datetime) -> bool:
        # The overlap window only extends the old plaintext's life up to the key's own
        # expiry, never past it.
        return (
            self.is_usable(now=now)
            and self.previous_key_hash is not None
            and self.previous_key_expires_at is not None
            and _aware(self.previous_key_expires_at) > now
        )

    def audit_snapshot(self) -> dict[str, Any]:
        """State recorded as an audit-log `before`/`after` (camelCase, like the API).
        Never includes `key_hash`/`previous_key_hash` — those are secrets, not
        auditable state."""
        return {
            "name": self.name,
            "keyPrefix": self.key_prefix,
            "scopes": list(self.scopes),
            "status": self.status,
            "expiresAt": self.expires_at.isoformat() if self.expires_at else None,
        }
