"""Base domain entity.

Plain dataclasses on purpose — the domain layer must stay importable
without SQLAlchemy/FastAPI installed (enforced by the import-linter
`forbidden` contract in pyproject.toml).
"""

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from app.shared.domain.identifiers import uuid7


@dataclass(kw_only=True)
class Entity:
    id: UUID = field(default_factory=uuid7)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Entity):
            return NotImplemented
        return type(self) is type(other) and self.id == other.id

    def __hash__(self) -> int:
        return hash((type(self), self.id))


@dataclass(kw_only=True)
class AuditedEntity(Entity):
    """Entity flavor for aggregates that need audit/soft-delete metadata
    surfaced in the domain (not just persistence)."""

    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: UUID | None = None
    updated_by: UUID | None = None
    deleted_by: UUID | None = None
    deleted_at: datetime | None = None

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


@dataclass(kw_only=True)
class TenantScopedEntity(AuditedEntity):
    tenant_id: UUID
