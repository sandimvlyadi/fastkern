from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import update_request_context
from app.modules.examples.domain.entities import Example, ExampleItem
from app.modules.examples.infrastructure.models import ExampleItemModel
from app.modules.examples.infrastructure.repository import ExampleRepository
from tests.helpers import TenantFactory


async def test_add_persists_items_via_relationship_cascade(
    db_session: AsyncSession, create_db_tenant: TenantFactory
) -> None:
    examples = ExampleRepository(db_session)
    tenant_id = await create_db_tenant()
    update_request_context(tenant_id=tenant_id)

    created = await examples.add(
        Example(
            tenant_id=tenant_id,
            name="Invoice #1",
            items=[ExampleItem(label="a"), ExampleItem(label="b")],
        )
    )
    assert {i.label for i in created.items} == {"a", "b"}

    fetched = await examples.get_by_id(created.id)
    assert fetched is not None
    assert {i.label for i in fetched.items} == {"a", "b"}


async def test_save_replaces_items_wholesale_deleting_orphans(
    db_session: AsyncSession, create_db_tenant: TenantFactory
) -> None:
    examples = ExampleRepository(db_session)
    tenant_id = await create_db_tenant()
    update_request_context(tenant_id=tenant_id)

    created = await examples.add(
        Example(
            tenant_id=tenant_id,
            name="Invoice #1",
            items=[ExampleItem(label="old1"), ExampleItem(label="old2")],
        )
    )
    old_item_ids = {i.id for i in created.items}

    entity = await examples.get_by_id(created.id)
    assert entity is not None
    entity.items = [ExampleItem(label="new1")]
    saved = await examples.save(entity)

    assert [i.label for i in saved.items] == ["new1"]

    refetched = await examples.get_by_id(created.id)
    assert refetched is not None
    assert [i.label for i in refetched.items] == ["new1"]

    # Prove delete-orphan actually fired (old rows gone), not just detached/unreferenced.
    remaining_old = (
        await db_session.execute(select(func.count()).where(ExampleItemModel.id.in_(old_item_ids)))
    ).scalar_one()
    assert remaining_old == 0


async def test_save_without_touching_items_preserves_item_identity(
    db_session: AsyncSession, create_db_tenant: TenantFactory
) -> None:
    examples = ExampleRepository(db_session)
    tenant_id = await create_db_tenant()
    update_request_context(tenant_id=tenant_id)

    created = await examples.add(
        Example(tenant_id=tenant_id, name="Invoice #1", items=[ExampleItem(label="a")])
    )
    original_item_id = created.items[0].id

    entity = await examples.get_by_id(created.id)
    assert entity is not None
    entity.name = "Invoice #1 (renamed)"  # items untouched, as a PATCH omitting `items` would do
    saved = await examples.save(entity)

    assert len(saved.items) == 1
    assert saved.items[0].id == original_item_id

    original_row = (
        await db_session.execute(
            select(ExampleItemModel).where(ExampleItemModel.id == original_item_id)
        )
    ).scalar_one()
    original_created_at = original_row.created_at

    # Save again (still not touching items) to prove the row wasn't deleted and recreated.
    saved_again = await examples.save(entity)
    assert saved_again.items[0].id == original_item_id
    refetched_row = (
        await db_session.execute(
            select(ExampleItemModel).where(ExampleItemModel.id == original_item_id)
        )
    ).scalar_one()
    assert refetched_row.created_at == original_created_at


async def test_save_clearing_all_items(
    db_session: AsyncSession, create_db_tenant: TenantFactory
) -> None:
    examples = ExampleRepository(db_session)
    tenant_id = await create_db_tenant()
    update_request_context(tenant_id=tenant_id)

    created = await examples.add(
        Example(tenant_id=tenant_id, name="Invoice #1", items=[ExampleItem(label="a")])
    )
    entity = await examples.get_by_id(created.id)
    assert entity is not None
    entity.items = []
    saved = await examples.save(entity)
    assert saved.items == []


async def test_delete_is_soft_and_does_not_fk_violate_master_example(
    db_session: AsyncSession, create_db_tenant: TenantFactory
) -> None:
    examples = ExampleRepository(db_session)
    tenant_id = await create_db_tenant()
    update_request_context(tenant_id=tenant_id)

    created = await examples.add(Example(tenant_id=tenant_id, name="Invoice #1"))
    await examples.delete(created.id)
    assert await examples.get_by_id(created.id) is None
    assert await examples.get_by_id(created.id, include_deleted=True) is not None
