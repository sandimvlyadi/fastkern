"""Generic async repository base.

Concrete repositories (one per aggregate, living in each module's
`infrastructure/repository.py`) subclass this, set `model`, and implement
`_to_entity`/`_to_model`. Soft-delete filtering and tenant scoping happen
here once, so no module can accidentally leak another tenant's rows or
resurrect a soft-deleted record by forgetting a `WHERE` clause.

Tenant scoping resolves, in order: an explicit `tenant_id=` override (for
the few pre-authentication flows — register/login — that must scope by a
client-supplied tenant rather than ambient request context), then
`get_request_context().tenant_id`. If a tenant-owned model is queried with
`tenant_scoped=True` (the default) and neither resolves, that is a bug —
we raise rather than silently returning every tenant's rows.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from typing import Any, ClassVar
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import get_request_context
from app.core.exceptions import AppError
from app.core.pagination import OffsetPage, OffsetParams
from app.shared.infrastructure.integrity import violated_unique_constraint
from app.shared.infrastructure.orm_base import Base

# Unique constraint/index name -> builds the domain error for the entity being written.
UniqueViolationMap = Mapping[str, Callable[[Any], AppError]]


class MissingTenantScopeError(RuntimeError):
    """Raised when a tenant-scoped query has no tenant to scope by.

    This is a programming-error guard, not a domain/business error: it
    means request-context setup (or an explicit override) was skipped
    somewhere upstream of a tenant-owned query. Never catch this to fall
    back to an unscoped query.
    """


class BaseRepository[ModelT: Base, EntityT](ABC):
    model: type[ModelT]

    unique_violations: ClassVar[UniqueViolationMap] = {}
    """Unique constraint/index name -> builder of the domain error to raise for it (given the
    entity being written). Lets `add`/`save` turn the race a check-then-insert leaves open
    into the same 409 the check would have produced. Constraints not listed re-raise as-is."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _flush(self, entity: EntityT) -> None:
        """Flush, mapping a known unique violation to its domain error.

        A failed flush leaves the session's transaction unusable ("rolled back due to a
        previous exception during flush"), so it is rolled back here: whoever catches the
        domain error (or the request's session cleanup) can then carry on without a
        `PendingRollbackError`. Callers must therefore treat a violation as failing the
        whole request — true of every 409 path, which raises straight to the handler."""
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            name = violated_unique_constraint(exc)
            build_error = self.unique_violations.get(name) if name else None
            if build_error is None:
                raise
            raise build_error(entity) from exc

    @abstractmethod
    def _to_entity(self, row: ModelT) -> EntityT: ...

    @abstractmethod
    def _to_model(self, entity: EntityT) -> ModelT: ...

    def _apply_scopes(
        self,
        stmt: Select[Any],
        *,
        include_deleted: bool,
        tenant_scoped: bool,
        tenant_id: UUID | None = None,
    ) -> Select[Any]:
        if not include_deleted and hasattr(self.model, "deleted_at"):
            stmt = stmt.where(self.model.deleted_at.is_(None))  # type: ignore[attr-defined]
        if tenant_scoped and hasattr(self.model, "tenant_id"):
            effective_tenant_id = (
                tenant_id if tenant_id is not None else get_request_context().tenant_id
            )
            if effective_tenant_id is None:
                raise MissingTenantScopeError(
                    f"{self.model.__name__}: a tenant-scoped query was attempted with no "
                    "tenant_id available (neither an explicit override nor "
                    "get_request_context().tenant_id). Pass tenant_id explicitly for "
                    "pre-authentication flows, or tenant_scoped=False if this query is "
                    "genuinely platform-wide."
                )
            stmt = stmt.where(self.model.tenant_id == effective_tenant_id)  # type: ignore[attr-defined]
        return stmt

    def _base_query(
        self,
        *,
        include_deleted: bool = False,
        tenant_scoped: bool = True,
        tenant_id: UUID | None = None,
    ) -> Select[Any]:
        return self._apply_scopes(
            select(self.model),
            include_deleted=include_deleted,
            tenant_scoped=tenant_scoped,
            tenant_id=tenant_id,
        )

    async def get_by_id(
        self,
        entity_id: UUID,
        *,
        include_deleted: bool = False,
        tenant_scoped: bool = True,
        tenant_id: UUID | None = None,
    ) -> EntityT | None:
        stmt = self._base_query(
            include_deleted=include_deleted, tenant_scoped=tenant_scoped, tenant_id=tenant_id
        ).where(self.model.id == entity_id)  # type: ignore[attr-defined]
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return self._to_entity(row) if row is not None else None

    @staticmethod
    def _current_actor() -> UUID | None:
        """The authenticated user behind this request, or None outside one
        (Celery task, seed script, pre-login flow such as registration)."""
        return get_request_context().user_id

    def _stamp_updated_by(self, row: object) -> None:
        """Set `updated_by` on a persistent row being modified.

        Explicit here (not a column `onupdate`): with no actor (Celery task,
        seed) `onupdate` would overwrite the previous updated_by with NULL,
        whereas here it is simply left alone.
        """
        actor_id = self._current_actor()
        if actor_id is not None and hasattr(row, "updated_by"):
            row.updated_by = actor_id

    async def add(self, entity: EntityT) -> EntityT:
        model = self._to_model(entity)
        actor_id = self._current_actor()
        # Stamp who created the row. An actor already on the entity wins, and
        # with no authenticated user the columns stay NULL rather than guess.
        if actor_id is not None and hasattr(model, "created_by"):
            if model.created_by is None:
                model.created_by = actor_id
            if model.updated_by is None:  # type: ignore[attr-defined]
                model.updated_by = actor_id  # type: ignore[attr-defined]
        self._session.add(model)
        await self._flush(entity)
        await self._session.refresh(model)
        return self._to_entity(model)

    async def save(
        self,
        entity: EntityT,
        *,
        tenant_scoped: bool = True,
        tenant_id: UUID | None = None,
    ) -> EntityT:
        """Writes a mutated entity (already fetched via `get_by_id` earlier
        in this same request/session) back onto its persistent row.

        Deliberately NOT `Session.merge()`: merge() can trigger an
        attribute refresh from the DB outside of SQLAlchemy's async
        greenlet context, raising `MissingGreenlet`, on top of it happily
        overwriting columns (like `created_at`) that `_to_model()` never
        populated in the first place.

        Routed through `_base_query()` like every other method — a raw
        `session.get(model, id)` lookup here would skip both the
        soft-delete filter and tenant scoping, so a caller that (now or in
        the future) doesn't re-validate the id via a tenant-scoped
        `get_by_id()` immediately beforehand could otherwise overwrite
        another tenant's row or resurrect a soft-deleted one.
        """
        model = self._to_model(entity)
        entity_id: UUID = model.id  # type: ignore[attr-defined]
        stmt = self._base_query(tenant_scoped=tenant_scoped, tenant_id=tenant_id).where(
            self.model.id == entity_id  # type: ignore[attr-defined]
        )
        existing = (await self._session.execute(stmt)).scalar_one_or_none()
        if existing is None:
            raise LookupError(f"{self.model.__name__} {entity_id} not found for save()")
        for column in self.model.__table__.columns.keys():  # noqa: SIM118 - ColumnCollection, not a dict
            # id/created_at never change; updated_at is server-managed
            # (onupdate=func.now()) and `_to_model()` never populates it on
            # a transient instance, so copying it here would overwrite the
            # trigger with a NULL and violate the NOT NULL constraint.
            if column in {"id", "created_at", "updated_at"}:
                continue
            setattr(existing, column, getattr(model, column))
        self._stamp_updated_by(existing)
        await self._flush(entity)
        await self._session.refresh(existing)
        return self._to_entity(existing)

    async def delete(
        self,
        entity_id: UUID,
        *,
        hard: bool = False,
        tenant_scoped: bool = True,
        tenant_id: UUID | None = None,
    ) -> None:
        """Deletes only a row within the current tenant scope — routed through
        `_base_query()` like every other read, so this can never reach across
        a tenant boundary the way a raw `session.get(model, id)` lookup could.

        A missing row is silently ignored. A use case that audits the deletion looks the
        row up first, so an unknown id is a 404 rather than a "deleted" event for nothing."""
        stmt = self._base_query(tenant_scoped=tenant_scoped, tenant_id=tenant_id).where(
            self.model.id == entity_id  # type: ignore[attr-defined]
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return
        if hard or not hasattr(row, "soft_delete"):
            await self._session.delete(row)
        else:
            # The trigger bumps updated_at for the soft delete, so updated_by
            # must move with it (deleted_by/deleted_at record the deletion itself).
            row.soft_delete(self._current_actor())
            self._stamp_updated_by(row)
        await self._session.flush()

    async def _paginate_query(self, base: Select[Any], params: OffsetParams) -> OffsetPage[EntityT]:
        """Shared count + offset/limit + envelope logic. `paginate()` below
        calls this with the plain `_base_query()`; a subclass that needs
        extra filters/ordering on top of tenant/soft-delete scoping (e.g.
        `AuditLogRepository.paginate()`) builds its own filtered `base` and
        calls this directly instead of re-implementing count/offset/limit
        by hand — keeps a future pagination fix (an off-by-one, cursor
        support, ...) applying everywhere at once."""
        total = (
            await self._session.execute(select(func.count()).select_from(base.subquery()))
        ).scalar_one()
        stmt = base.offset(params.offset).limit(params.page_size)
        rows = (await self._session.execute(stmt)).scalars().all()
        return OffsetPage(
            items=[self._to_entity(r) for r in rows],
            total=total,
            page=params.page,
            page_size=params.page_size,
        )

    async def paginate(
        self,
        params: OffsetParams,
        *,
        include_deleted: bool = False,
        tenant_scoped: bool = True,
        tenant_id: UUID | None = None,
    ) -> OffsetPage[EntityT]:
        base = self._base_query(
            include_deleted=include_deleted, tenant_scoped=tenant_scoped, tenant_id=tenant_id
        )
        return await self._paginate_query(base, params)
