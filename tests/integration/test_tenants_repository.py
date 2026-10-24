from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.tenants.domain.entities import Tenant
from app.modules.tenants.domain.exceptions import TenantSlugAlreadyExistsError
from app.modules.tenants.infrastructure.repository import TenantRepository


async def test_add_and_get_by_id(db_session: AsyncSession) -> None:
    tenants = TenantRepository(db_session)
    tenant = await tenants.add(Tenant(name="Acme", slug=f"acme-{uuid4().hex[:8]}"))

    found = await tenants.get_by_id(tenant.id)
    assert found is not None
    assert found.name == "Acme"


async def test_get_by_slug_hit_and_miss(db_session: AsyncSession) -> None:
    tenants = TenantRepository(db_session)
    slug = f"acme-{uuid4().hex[:8]}"
    await tenants.add(Tenant(name="Acme", slug=slug))

    assert (await tenants.get_by_slug(slug)) is not None
    assert (await tenants.get_by_slug("does-not-exist")) is None


async def test_list_all_contains_new_tenant(db_session: AsyncSession) -> None:
    tenants = TenantRepository(db_session)
    tenant = await tenants.add(Tenant(name="Acme", slug=f"acme-{uuid4().hex[:8]}"))

    all_tenants = await tenants.list_all()
    assert any(t.id == tenant.id for t in all_tenants)


async def test_tenant_slug_is_unique_among_live_rows(db_session: AsyncSession) -> None:
    tenants = TenantRepository(db_session)
    slug = f"acme-{uuid4().hex[:8]}"
    await tenants.add(Tenant(name="Acme", slug=slug))

    with pytest.raises(TenantSlugAlreadyExistsError) as raised:
        await tenants.add(Tenant(name="Acme 2", slug=slug))
    assert raised.value.context == {"slug": slug}


async def test_tenant_slug_is_reusable_after_soft_delete(db_session: AsyncSession) -> None:
    tenants = TenantRepository(db_session)
    slug = f"acme-{uuid4().hex[:8]}"
    first = await tenants.add(Tenant(name="Acme", slug=slug))

    await tenants.delete(first.id)
    second = await tenants.add(Tenant(name="Acme again", slug=slug))

    assert await tenants.get_by_id(first.id) is None
    assert (await tenants.get_by_slug(slug)) == second


async def test_tenant_save_preserves_created_by_and_updated_by(db_session: AsyncSession) -> None:
    tenants = TenantRepository(db_session)
    actor_id = uuid4()
    tenant = await tenants.add(
        Tenant(
            name="Acme", slug=f"acme-{uuid4().hex[:8]}", created_by=actor_id, updated_by=actor_id
        )
    )

    tenant.name = "Acme Holdings"
    saved = await tenants.save(tenant)

    assert saved.name == "Acme Holdings"
    assert (saved.created_by, saved.updated_by) == (actor_id, actor_id)
