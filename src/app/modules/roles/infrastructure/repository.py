from typing import Any, ClassVar, cast
from uuid import UUID

from sqlalchemy import CursorResult, Select, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import get_request_context
from app.core.pagination import OffsetPage, OffsetParams
from app.modules.roles.domain.entities import Role, UserRoleAssignment
from app.modules.roles.domain.exceptions import RoleAlreadyExistsError
from app.modules.roles.domain.ports import RoleRepositoryPort, UserRoleRepositoryPort
from app.modules.roles.infrastructure.models import RoleModel, UserRoleModel
from app.shared.infrastructure.integrity import violated_unique_constraint
from app.shared.infrastructure.repository import (
    BaseRepository,
    MissingTenantScopeError,
    UniqueViolationMap,
)

_DEFAULT_UNIQUE_INDEX = "uq_sys_user_roles_tenant_user_default"


def _role_to_entity(row: RoleModel) -> Role:
    return Role(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        description=row.description,
        permissions=list(row.permissions or []),
        is_system=row.is_system,
        created_at=row.created_at,
        updated_at=row.updated_at,
        created_by=row.created_by,
        updated_by=row.updated_by,
        deleted_by=row.deleted_by,
        deleted_at=row.deleted_at,
    )


class RoleRepository(BaseRepository[RoleModel, Role], RoleRepositoryPort):
    model = RoleModel
    unique_violations: ClassVar[UniqueViolationMap] = {
        "uq_sys_roles_tenant_name": lambda role: RoleAlreadyExistsError(name=role.name),
    }

    def _to_entity(self, row: RoleModel) -> Role:
        return _role_to_entity(row)

    def _to_model(self, entity: Role) -> RoleModel:
        return RoleModel(
            id=entity.id,
            tenant_id=entity.tenant_id,
            name=entity.name,
            description=entity.description,
            permissions=entity.permissions,
            is_system=entity.is_system,
            # save() copies every column, so these must round-trip or an
            # edit would overwrite them with NULL.
            created_by=entity.created_by,
            updated_by=entity.updated_by,
        )

    async def get_by_name(self, name: str, *, tenant_id: UUID | None = None) -> Role | None:
        stmt = self._base_query(tenant_id=tenant_id).where(self.model.name == name)
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return self._to_entity(row) if row is not None else None

    async def list_all(self) -> list[Role]:
        rows = (await self._session.execute(self._base_query())).scalars().all()
        return [self._to_entity(r) for r in rows]


class UserRoleRepository(UserRoleRepositoryPort):
    """Association-table repository for the user<->role many-to-many.

    Every query here filters by tenant_id explicitly (this table isn't a
    `BaseRepository` subclass since it has no single-aggregate identity to
    key `get_by_id`/`paginate` off), matching the same tenant-scoping
    guarantee `BaseRepository._apply_scopes` gives every other repository.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _tenant_id(self, tenant_id: UUID | None = None) -> UUID:
        effective_tenant_id = (
            tenant_id if tenant_id is not None else get_request_context().tenant_id
        )
        if effective_tenant_id is None:
            raise MissingTenantScopeError(
                "UserRoleRepository: a tenant-scoped operation was attempted with no "
                "tenant_id available (neither an explicit override nor "
                "get_request_context().tenant_id). Pass tenant_id explicitly for "
                "pre-authentication flows (register/login)."
            )
        return effective_tenant_id

    def _roles_for_user_query(self, user_id: UUID, tenant_id: UUID) -> Select[Any]:
        return (
            select(RoleModel)
            .join(UserRoleModel, UserRoleModel.role_id == RoleModel.id)
            .where(
                UserRoleModel.tenant_id == tenant_id,
                UserRoleModel.user_id == user_id,
                RoleModel.tenant_id == tenant_id,
                RoleModel.deleted_at.is_(None),
            )
            # Deterministic ordering matters beyond display: LoginUseCase
            # picks roles[0] as the JWT's active_role claim, and an
            # unordered join can return a different "first" role across
            # otherwise-identical queries.
            .order_by(RoleModel.name)
        )

    def _role_assignments_for_user_query(
        self, user_id: UUID, tenant_id: UUID
    ) -> Select[RoleModel, bool]:
        return (
            select(RoleModel, UserRoleModel.is_default)
            .join(UserRoleModel, UserRoleModel.role_id == RoleModel.id)
            .where(
                UserRoleModel.tenant_id == tenant_id,
                UserRoleModel.user_id == user_id,
                RoleModel.tenant_id == tenant_id,
                RoleModel.deleted_at.is_(None),
            )
            .order_by(RoleModel.name)
        )

    async def assign(
        self,
        user_id: UUID,
        role_id: UUID,
        *,
        tenant_id: UUID | None = None,
        is_default: bool = False,
    ) -> bool:
        effective_tenant_id = self._tenant_id(tenant_id)
        # One atomic statement instead of check-then-insert: two concurrent grants of the same
        # role both pass a SELECT, and the loser's INSERT would violate the unique key (a 500).
        # DO NOTHING leaves the session's transaction intact and makes the outcome exactly the
        # idempotent one: the row exists afterwards, and only one caller "created" it.
        insert = (
            postgresql_insert
            if self._session.get_bind().dialect.name == "postgresql"
            else (sqlite_insert)
        )

        def _stmt(default: bool) -> Any:
            return (
                insert(UserRoleModel)
                .values(
                    user_id=user_id,
                    role_id=role_id,
                    tenant_id=effective_tenant_id,
                    is_default=default,
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "user_id", "role_id"])
            )

        try:
            result = await self._session.execute(_stmt(is_default))
            await self._session.flush()
        except IntegrityError as exc:
            # ON CONFLICT above only covers the *same* role being assigned twice. Two
            # concurrent "this is the user's first role" assignments of *different* roles
            # both see zero existing roles and both pass is_default=True; the partial
            # unique index `uq_sys_user_roles_tenant_user_default` then catches the
            # second one. Same recovery as BaseRepository._flush: roll back and retry as
            # a non-default assignment instead of surfacing a 500 — another role already
            # won being "first", which is a fine, idempotent-ish outcome.
            await self._session.rollback()
            if violated_unique_constraint(exc) != _DEFAULT_UNIQUE_INDEX:
                raise
            result = await self._session.execute(_stmt(False))
            await self._session.flush()
        return cast("CursorResult[Any]", result).rowcount > 0

    async def revoke(self, user_id: UUID, role_id: UUID, *, tenant_id: UUID | None = None) -> bool:
        effective_tenant_id = self._tenant_id(tenant_id)
        stmt = delete(UserRoleModel).where(
            UserRoleModel.tenant_id == effective_tenant_id,
            UserRoleModel.user_id == user_id,
            UserRoleModel.role_id == role_id,
        )
        result = cast("CursorResult[Any]", await self._session.execute(stmt))
        await self._session.flush()
        return result.rowcount > 0

    async def list_roles_for_user(
        self, user_id: UUID, *, tenant_id: UUID | None = None
    ) -> list[Role]:
        effective_tenant_id = self._tenant_id(tenant_id)
        stmt = self._roles_for_user_query(user_id, effective_tenant_id)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_role_to_entity(r) for r in rows]

    async def paginate_roles_for_user(
        self, user_id: UUID, params: OffsetParams, *, tenant_id: UUID | None = None
    ) -> OffsetPage[UserRoleAssignment]:
        effective_tenant_id = self._tenant_id(tenant_id)
        base = self._role_assignments_for_user_query(user_id, effective_tenant_id)
        total = (
            await self._session.execute(select(func.count()).select_from(base.subquery()))
        ).scalar_one()
        rows = (
            await self._session.execute(base.offset(params.offset).limit(params.page_size))
        ).all()
        return OffsetPage(
            items=[
                UserRoleAssignment(role=_role_to_entity(role_row), is_default=is_default)
                for role_row, is_default in rows
            ],
            total=total,
            page=params.page,
            page_size=params.page_size,
        )

    async def get_default_role_for_user(
        self, user_id: UUID, *, tenant_id: UUID | None = None
    ) -> Role | None:
        effective_tenant_id = self._tenant_id(tenant_id)
        stmt = (
            select(RoleModel)
            .join(UserRoleModel, UserRoleModel.role_id == RoleModel.id)
            .where(
                UserRoleModel.tenant_id == effective_tenant_id,
                UserRoleModel.user_id == user_id,
                UserRoleModel.is_default.is_(True),
                RoleModel.tenant_id == effective_tenant_id,
                RoleModel.deleted_at.is_(None),
            )
        )
        row = (await self._session.execute(stmt)).scalars().first()
        return _role_to_entity(row) if row is not None else None

    async def set_default_role(
        self, user_id: UUID, role_id: UUID, *, tenant_id: UUID | None = None
    ) -> bool:
        effective_tenant_id = self._tenant_id(tenant_id)
        # Check-then-update (not fully atomic against a concurrent revoke of
        # this exact role): same TOCTOU profile as CreateRoleUseCase's
        # get_by_name-then-add, with the partial unique index on
        # sys_user_roles as the DB-level backstop.
        target = await self._session.execute(
            select(UserRoleModel.id).where(
                UserRoleModel.tenant_id == effective_tenant_id,
                UserRoleModel.user_id == user_id,
                UserRoleModel.role_id == role_id,
            )
        )
        if target.scalar_one_or_none() is None:
            return False
        await self._session.execute(
            update(UserRoleModel)
            .where(
                UserRoleModel.tenant_id == effective_tenant_id,
                UserRoleModel.user_id == user_id,
            )
            .values(is_default=(UserRoleModel.role_id == role_id))
        )
        await self._session.flush()
        return True

    async def user_has_role(
        self, user_id: UUID, role_name: str, *, tenant_id: UUID | None = None
    ) -> bool:
        roles = await self.list_roles_for_user(user_id, tenant_id=tenant_id)
        return any(r.name == role_name for r in roles)
