from typing import Any
from uuid import UUID

from sqlalchemy import Select
from sqlalchemy.orm import selectinload

from app.modules.examples.domain.entities import Example, ExampleItem
from app.modules.examples.domain.ports import ExampleRepositoryPort
from app.modules.examples.infrastructure.models import ExampleItemModel, ExampleModel
from app.shared.infrastructure.repository import BaseRepository


class ExampleRepository(BaseRepository[ExampleModel, Example], ExampleRepositoryPort):
    model = ExampleModel

    def _base_query(self, **kwargs: Any) -> Select[Any]:
        return super()._base_query(**kwargs).options(selectinload(self.model.items))

    def _to_entity(self, row: ExampleModel) -> Example:
        return Example(
            id=row.id,
            tenant_id=row.tenant_id,
            name=row.name,
            description=row.description,
            master_example_id=row.master_example_id,
            items=[ExampleItem(id=i.id, label=i.label) for i in row.items],
            created_at=row.created_at,
            updated_at=row.updated_at,
            created_by=row.created_by,
            updated_by=row.updated_by,
            deleted_by=row.deleted_by,
            deleted_at=row.deleted_at,
        )

    def _to_model(self, entity: Example) -> ExampleModel:
        return ExampleModel(
            id=entity.id,
            tenant_id=entity.tenant_id,
            name=entity.name,
            description=entity.description,
            master_example_id=entity.master_example_id,
            items=[
                ExampleItemModel(label=i.label, tenant_id=entity.tenant_id) for i in entity.items
            ],
            created_by=entity.created_by,
            updated_by=entity.updated_by,
        )

    async def _refresh_including_items(self, model: ExampleModel) -> None:
        """`Session.refresh()` with no `attribute_names` expires every attribute, including
        the `items` relationship — the very next `self._to_entity()` call then tries to
        lazily reload `model.items` as plain synchronous attribute access, which raises
        `sqlalchemy.exc.MissingGreenlet` under the async engine (no greenlet bridge is active
        outside an actual `await`). A second, explicit `refresh(..., attribute_names=["items"])`
        is itself a proper async-bridged call, so it reloads the collection safely."""
        await self._session.refresh(model)
        await self._session.refresh(model, attribute_names=["items"])

    async def add(self, entity: Example) -> Example:
        """Overrides BaseRepository.add() in full (not just extends it) for the same
        MissingGreenlet reason `save()` below is overridden — see `_refresh_including_items`.
        SQLAlchemy's default relationship cascade does insert the transient `items` set by
        `_to_model()` when the parent is added; the override exists only to refresh safely
        afterwards, not to change how the insert itself happens."""
        model = self._to_model(entity)
        actor_id = self._current_actor()
        if actor_id is not None:
            if model.created_by is None:
                model.created_by = actor_id
            if model.updated_by is None:
                model.updated_by = actor_id
        self._session.add(model)
        await self._flush(entity)
        await self._refresh_including_items(model)
        return self._to_entity(model)

    @staticmethod
    def _merge_items(existing: ExampleModel, items: list[ExampleItem]) -> None:
        """Reconcile `existing.items` with `items` by id: matching ids are updated in place
        (keeping id and created_*), missing ids are deleted (`delete-orphan`), new ids are
        inserted. An update that leaves `items` untouched therefore changes no item rows; a
        new list (fresh ids) replaces them all."""
        existing_by_id = {row.id: row for row in existing.items}
        merged: list[ExampleItemModel] = []
        for item in items:
            row = existing_by_id.get(item.id)
            if row is not None:
                row.label = item.label
                merged.append(row)
            else:
                merged.append(ExampleItemModel(label=item.label, tenant_id=existing.tenant_id))
        existing.items = merged

    async def save(
        self, entity: Example, *, tenant_scoped: bool = True, tenant_id: UUID | None = None
    ) -> Example:
        """Overrides BaseRepository.save() in full (not just extends it): the base
        implementation only copies `self.model.__table__.columns`, which does not include
        the `items` relationship — inheriting it as-is would silently drop item edits on
        every update. `_merge_items()` reconciles `.items` by id rather than blindly
        reassigning it, so an update that leaves the item list untouched doesn't churn every
        item's id/created_at/created_by."""
        model = self._to_model(entity)
        stmt = self._base_query(tenant_scoped=tenant_scoped, tenant_id=tenant_id).where(
            self.model.id == model.id
        )
        existing = (await self._session.execute(stmt)).scalar_one_or_none()
        if existing is None:
            raise LookupError(f"{self.model.__name__} {model.id} not found for save()")
        for column in self.model.__table__.columns.keys():  # noqa: SIM118
            if column in {"id", "created_at", "updated_at"}:
                continue
            setattr(existing, column, getattr(model, column))
        self._merge_items(existing, entity.items)
        self._stamp_updated_by(existing)
        await self._flush(entity)
        await self._refresh_including_items(existing)
        return self._to_entity(existing)
