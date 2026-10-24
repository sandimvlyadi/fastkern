from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from app.shared.domain.identifiers import uuid7


@dataclass(kw_only=True, frozen=True, slots=True)
class AuditLogEntry:
    """Append-only audit trail entry. Never updated or soft-deleted once
    written — audit history integrity is the whole point of this table."""

    id: UUID = field(default_factory=uuid7)
    tenant_id: UUID | None
    actor_id: UUID | None
    actor_type: str = "user"
    """Kind of actor: "user" for JWT/RBAC-authenticated requests, "api_client" for
    API-key-authenticated partner requests. Defaults to "user" to match the column's
    server_default, since most audit events come from an authenticated user."""
    action: str
    resource_type: str
    resource_id: str | None
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    request_id: str | None = None
    ip_address: str | None = None
    created_at: datetime | None = None
