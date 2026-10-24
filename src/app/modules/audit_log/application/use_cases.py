from uuid import UUID

from app.core.pagination import OffsetPage, OffsetParams
from app.modules.audit_log.application.dto import RecordAuditEventInput
from app.modules.audit_log.domain.entities import AuditLogEntry
from app.modules.audit_log.domain.ports import AuditLogRepositoryPort


class RecordAuditEventUseCase:
    def __init__(self, audit_logs: AuditLogRepositoryPort) -> None:
        self._audit_logs = audit_logs

    async def execute(self, data: RecordAuditEventInput) -> AuditLogEntry:
        entry = AuditLogEntry(
            tenant_id=data.tenant_id,
            actor_id=data.actor_id,
            actor_type=data.actor_type,
            action=data.action,
            resource_type=data.resource_type,
            resource_id=data.resource_id,
            before=data.before,
            after=data.after,
            request_id=data.request_id,
            ip_address=data.ip_address,
        )
        return await self._audit_logs.record(entry)


class ListAuditLogsUseCase:
    def __init__(self, audit_logs: AuditLogRepositoryPort) -> None:
        self._audit_logs = audit_logs

    async def execute(
        self,
        params: OffsetParams,
        *,
        resource_type: str | None = None,
        actor_id: UUID | None = None,
    ) -> OffsetPage[AuditLogEntry]:
        return await self._audit_logs.paginate(
            params, resource_type=resource_type, actor_id=actor_id
        )
