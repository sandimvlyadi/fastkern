from abc import ABC, abstractmethod
from uuid import UUID

from app.core.pagination import OffsetPage, OffsetParams
from app.modules.audit_log.domain.entities import AuditLogEntry


class AuditLogRepositoryPort(ABC):
    @abstractmethod
    async def record(self, entry: AuditLogEntry) -> AuditLogEntry: ...

    @abstractmethod
    async def paginate(
        self,
        params: OffsetParams,
        *,
        resource_type: str | None = None,
        actor_id: UUID | None = None,
    ) -> OffsetPage[AuditLogEntry]: ...
