from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import update_request_context
from app.modules.api_clients.domain.entities import ApiKey
from app.modules.api_clients.domain.hashing import generate_api_key
from app.modules.api_clients.infrastructure.repository import ApiKeyRepository
from tests.helpers import TenantFactory


def _api_key(tenant_id: object) -> ApiKey:
    _, key_prefix, key_hash = generate_api_key()
    return ApiKey(
        tenant_id=tenant_id,  # type: ignore[arg-type]
        name="Test Key",
        key_prefix=key_prefix,
        key_hash=key_hash,
        scopes=["posts:read"],
    )


async def test_save_never_wipes_created_by(
    db_session: AsyncSession, create_db_tenant: TenantFactory
) -> None:
    """Regression test: `ApiKeyRepository._to_model()` must forward `created_by` (and the
    other actor/soft-delete columns) through to every `save()`, same as `UserRepository` —
    see `test_base_repository.py::test_save_stamps_updated_by_but_never_rewrites_created_by`.
    Omitting them silently nulled out `created_by` on the very first rotate/revoke/verify
    `save()` call after creation."""
    tenant_id = await create_db_tenant()
    creator = uuid4()
    update_request_context(tenant_id=tenant_id, user_id=creator)

    repo = ApiKeyRepository(db_session)
    api_key = await repo.add(_api_key(tenant_id))
    assert api_key.created_by == creator

    # Simulate VerifyApiKeyUseCase's save() with no authenticated user in context (a
    # partner-authenticated request never has a `user_id`).
    update_request_context(user_id=None)
    api_key.last_used_at = None
    saved = await repo.save(api_key, tenant_id=tenant_id)

    assert saved.created_by == creator


async def test_save_stamps_updated_by_but_never_rewrites_created_by(
    db_session: AsyncSession, create_db_tenant: TenantFactory
) -> None:
    """Same shape as `test_base_repository.py`'s identically-named test for `UserRepository`."""
    tenant_id = await create_db_tenant()
    creator, editor = uuid4(), uuid4()
    repo = ApiKeyRepository(db_session)
    update_request_context(tenant_id=tenant_id, user_id=creator)
    api_key = await repo.add(_api_key(tenant_id))

    update_request_context(user_id=editor)
    api_key.name = "Renamed"
    saved = await repo.save(api_key, tenant_id=tenant_id)
    assert (saved.created_by, saved.updated_by) == (creator, editor)
