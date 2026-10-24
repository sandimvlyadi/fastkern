from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import ConfigDict

from app.core.config import get_settings
from app.shared.application.dto import CamelModel

_DEFAULT_TENANT_ID = get_settings().default_tenant_id


class AuditLogEntryResponse(CamelModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": "5e2a1f3b-7c6d-4e8a-9f1b-2d3c4e5f6a7b",
                    "tenantId": _DEFAULT_TENANT_ID,
                    "actorId": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                    "actorType": "user",
                    "action": "user.updated",
                    "resourceType": "user",
                    "resourceId": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                    "before": {"fullName": "Jane Doe"},
                    "after": {"fullName": "Jane D. Smith"},
                    "requestId": "3c7a1e2b-0f4d-4a6b-8c2e-1a2b3c4d5e6f",
                    "createdAt": "2026-03-02T10:12:45Z",
                }
            ]
        }
    )

    id: UUID
    tenant_id: UUID
    actor_id: UUID | None
    actor_type: str
    action: str
    resource_type: str
    resource_id: str | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    request_id: str | None
    created_at: datetime
