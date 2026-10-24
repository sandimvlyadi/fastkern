from dataclasses import dataclass
from typing import Any
from uuid import UUID


@dataclass(frozen=True, slots=True)
class RecordAuditEventInput:
    tenant_id: UUID | None
    actor_id: UUID | None
    action: str
    resource_type: str
    resource_id: str | None
    actor_type: str = "user"
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    request_id: str | None = None
    ip_address: str | None = None
