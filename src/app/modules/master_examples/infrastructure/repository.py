from typing import ClassVar
from uuid import UUID

from app.modules.master_examples.domain.entities import MasterExample
from app.modules.master_examples.domain.exceptions import MasterExampleAlreadyExistsError
from app.modules.master_examples.domain.ports import MasterExampleRepositoryPort
from app.modules.master_examples.infrastructure.models import MasterExampleModel
from app.shared.infrastructure.repository import BaseRepository, UniqueViolationMap


class MasterExampleRepository(
    BaseRepository[MasterExampleModel, MasterExample], MasterExampleRepositoryPort
):
    model = MasterExampleModel
    unique_violations: ClassVar[UniqueViolationMap] = {
        "uq_master_examples_tenant_name": lambda e: MasterExampleAlreadyExistsError(name=e.name),
    }

    def _to_entity(self, row: MasterExampleModel) -> MasterExample:
        return MasterExample(
            id=row.id,
            tenant_id=row.tenant_id,
            name=row.name,
            description=row.description,
            created_at=row.created_at,
            updated_at=row.updated_at,
            created_by=row.created_by,
            updated_by=row.updated_by,
            deleted_by=row.deleted_by,
            deleted_at=row.deleted_at,
        )

    def _to_model(self, entity: MasterExample) -> MasterExampleModel:
        return MasterExampleModel(
            id=entity.id,
            tenant_id=entity.tenant_id,
            name=entity.name,
            description=entity.description,
            # save() copies every column, so these must round-trip or an edit would
            # overwrite them with NULL.
            created_by=entity.created_by,
            updated_by=entity.updated_by,
        )

    async def get_by_name(
        self, name: str, *, tenant_id: UUID | None = None
    ) -> MasterExample | None:
        stmt = self._base_query(tenant_id=tenant_id).where(self.model.name == name)
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return self._to_entity(row) if row is not None else None
