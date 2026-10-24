from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class CreateApiKeyInput:
    tenant_id: UUID
    name: str
    scopes: list[str] = field(default_factory=list)
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ApiKeyScopeEntry:
    """One catalog scope as seen by a specific caller: `grantable` is whether they could put it
    on a key without a `ScopeEscalationError`."""

    code: str
    resource: str
    action: str
    grantable: bool
