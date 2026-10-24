from typing import ClassVar
from uuid import UUID

from app.core.pagination import OffsetPage, OffsetParams
from app.modules.tenants.domain.entities import Tenant
from app.modules.tenants.domain.exceptions import TenantSlugAlreadyExistsError
from app.modules.tenants.domain.ports import TenantRepositoryPort
from app.modules.tenants.infrastructure.models import TenantModel
from app.shared.infrastructure.repository import BaseRepository, UniqueViolationMap


class TenantRepository(BaseRepository[TenantModel, Tenant], TenantRepositoryPort):
    model = TenantModel
    unique_violations: ClassVar[UniqueViolationMap] = {
        "uq_sys_tenants_slug": lambda tenant: TenantSlugAlreadyExistsError(slug=tenant.slug),
    }

    def _to_entity(self, row: TenantModel) -> Tenant:
        return Tenant(
            id=row.id,
            name=row.name,
            slug=row.slug,
            is_active=row.is_active,
            created_at=row.created_at,
            updated_at=row.updated_at,
            created_by=row.created_by,
            updated_by=row.updated_by,
            deleted_by=row.deleted_by,
            deleted_at=row.deleted_at,
        )

    def _to_model(self, entity: Tenant) -> TenantModel:
        # created_by/updated_by must round-trip: save() copies every column,
        # so omitting them would overwrite them with NULL on each edit.
        return TenantModel(
            id=entity.id,
            name=entity.name,
            slug=entity.slug,
            is_active=entity.is_active,
            created_by=entity.created_by,
            updated_by=entity.updated_by,
        )

    async def get_by_slug(self, slug: str) -> Tenant | None:
        stmt = self._base_query(tenant_scoped=False).where(self.model.slug == slug)
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return self._to_entity(row) if row is not None else None

    async def list_all(self) -> list[Tenant]:
        rows = (await self._session.execute(self._base_query(tenant_scoped=False))).scalars().all()
        return [self._to_entity(r) for r in rows]

    async def paginate(self, params: OffsetParams) -> OffsetPage[Tenant]:  # type: ignore[override]
        return await super().paginate(params, tenant_scoped=False)

    async def get_by_id(self, tenant_id: UUID) -> Tenant | None:  # type: ignore[override]
        return await super().get_by_id(tenant_id, tenant_scoped=False)

    async def delete(self, tenant_id: UUID) -> None:  # type: ignore[override]
        await super().delete(tenant_id, tenant_scoped=False)
