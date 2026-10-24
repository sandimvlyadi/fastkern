from typing import ClassVar
from uuid import UUID

from app.modules.users.domain.email import normalize_email
from app.modules.users.domain.entities import User
from app.modules.users.domain.exceptions import EmailAlreadyRegisteredError
from app.modules.users.domain.ports import UserRepositoryPort
from app.modules.users.infrastructure.models import UserModel
from app.shared.infrastructure.repository import BaseRepository, UniqueViolationMap


class UserRepository(BaseRepository[UserModel, User], UserRepositoryPort):
    model = UserModel
    unique_violations: ClassVar[UniqueViolationMap] = {
        "uq_sys_users_tenant_email": lambda user: EmailAlreadyRegisteredError(email=user.email),
    }

    def _to_entity(self, row: UserModel) -> User:
        return User(
            id=row.id,
            tenant_id=row.tenant_id,
            email=row.email,
            hashed_password=row.hashed_password,
            full_name=row.full_name,
            is_active=row.is_active,
            locale=row.locale,
            timezone=row.timezone,
            avatar_file_id=row.avatar_file_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
            created_by=row.created_by,
            updated_by=row.updated_by,
            deleted_by=row.deleted_by,
            deleted_at=row.deleted_at,
        )

    def _to_model(self, entity: User) -> UserModel:
        return UserModel(
            id=entity.id,
            tenant_id=entity.tenant_id,
            email=entity.email,
            hashed_password=entity.hashed_password,
            full_name=entity.full_name,
            is_active=entity.is_active,
            locale=entity.locale,
            timezone=entity.timezone,
            avatar_file_id=entity.avatar_file_id,
            created_by=entity.created_by,
            updated_by=entity.updated_by,
            deleted_by=entity.deleted_by,
            deleted_at=entity.deleted_at,
        )

    async def get_by_email(self, email: str, *, tenant_id: UUID) -> User | None:
        stmt = self._base_query(tenant_id=tenant_id).where(
            self.model.email == normalize_email(email)
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return self._to_entity(row) if row is not None else None
