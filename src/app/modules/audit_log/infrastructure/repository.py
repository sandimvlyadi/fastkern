from uuid import UUID

from app.core.pagination import OffsetPage, OffsetParams
from app.modules.audit_log.domain.entities import AuditLogEntry
from app.modules.audit_log.domain.ports import AuditLogRepositoryPort
from app.modules.audit_log.infrastructure.models import AuditLogModel
from app.shared.infrastructure.repository import BaseRepository


class AuditLogRepository(BaseRepository[AuditLogModel, AuditLogEntry], AuditLogRepositoryPort):
    """Subclasses `BaseRepository` so `paginate()` (via `_base_query()`)
    gets the same tenant scoping every other repository gets for free —
    without it, GET /audit-logs would leak every tenant's log entries to
    any caller holding `audit_log:read`."""

    model = AuditLogModel

    def _to_entity(self, row: AuditLogModel) -> AuditLogEntry:
        return AuditLogEntry(
            id=row.id,
            tenant_id=row.tenant_id,
            actor_id=row.actor_id,
            actor_type=row.actor_type,
            action=row.action,
            resource_type=row.resource_type,
            resource_id=row.resource_id,
            before=row.before,
            after=row.after,
            request_id=row.request_id,
            ip_address=row.ip_address,
            created_at=row.created_at,
        )

    def _to_model(self, entity: AuditLogEntry) -> AuditLogModel:
        return AuditLogModel(
            id=entity.id,
            tenant_id=entity.tenant_id,
            actor_id=entity.actor_id,
            actor_type=entity.actor_type,
            action=entity.action,
            resource_type=entity.resource_type,
            resource_id=entity.resource_id,
            before=entity.before,
            after=entity.after,
            request_id=entity.request_id,
            ip_address=entity.ip_address,
        )

    async def record(self, entry: AuditLogEntry) -> AuditLogEntry:
        return await self.add(entry)

    async def paginate(  # type: ignore[override]
        self,
        params: OffsetParams,
        *,
        resource_type: str | None = None,
        actor_id: UUID | None = None,
    ) -> OffsetPage[AuditLogEntry]:
        base = self._base_query().order_by(self.model.created_at.desc())
        if resource_type:
            base = base.where(self.model.resource_type == resource_type)
        if actor_id:
            base = base.where(self.model.actor_id == actor_id)
        return await self._paginate_query(base, params)
