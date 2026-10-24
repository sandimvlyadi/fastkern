from sqlalchemy import or_

from app.modules.api_clients.domain.entities import ApiKey
from app.modules.api_clients.domain.ports import ApiKeyRepositoryPort
from app.modules.api_clients.infrastructure.models import ApiKeyModel
from app.shared.infrastructure.repository import BaseRepository


class ApiKeyRepository(BaseRepository[ApiKeyModel, ApiKey], ApiKeyRepositoryPort):
    model = ApiKeyModel

    def _to_entity(self, row: ApiKeyModel) -> ApiKey:
        return ApiKey(
            id=row.id,
            tenant_id=row.tenant_id,
            name=row.name,
            key_prefix=row.key_prefix,
            key_hash=row.key_hash,
            previous_key_hash=row.previous_key_hash,
            previous_key_expires_at=row.previous_key_expires_at,
            scopes=list(row.scopes),
            status=row.status,  # type: ignore[arg-type]
            expires_at=row.expires_at,
            last_used_at=row.last_used_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
            created_by=row.created_by,
            updated_by=row.updated_by,
            deleted_by=row.deleted_by,
            deleted_at=row.deleted_at,
        )

    def _to_model(self, entity: ApiKey) -> ApiKeyModel:
        return ApiKeyModel(
            id=entity.id,
            tenant_id=entity.tenant_id,
            name=entity.name,
            key_prefix=entity.key_prefix,
            key_hash=entity.key_hash,
            previous_key_hash=entity.previous_key_hash,
            previous_key_expires_at=entity.previous_key_expires_at,
            scopes=list(entity.scopes),
            status=entity.status,
            expires_at=entity.expires_at,
            last_used_at=entity.last_used_at,
            created_by=entity.created_by,
            updated_by=entity.updated_by,
            deleted_by=entity.deleted_by,
            deleted_at=entity.deleted_at,
        )

    async def get_by_key_hash(self, key_hash: str) -> ApiKey | None:
        # Deliberately NOT tenant-scoped: see ApiKeyRepositoryPort.get_by_key_hash docstring.
        stmt = self._base_query(tenant_scoped=False).where(
            or_(self.model.key_hash == key_hash, self.model.previous_key_hash == key_hash)
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return self._to_entity(row) if row is not None else None

    # `paginate()` is inherited from BaseRepository unchanged (no extra filters needed yet).
