"""Regression test: `ApiKeyRepository._to_model()` must forward every actor/soft-delete
column (`created_by`, `updated_by`, `deleted_by`, `deleted_at`), same as `UserRepository`
does. `BaseRepository.save()` copies every table column from the transient model `_to_model()`
builds onto the persisted row — omitting one of these silently nulls it out on every `save()`
call, which bit `created_by` specifically (see `test_api_clients_repository.py`'s integration
regression test for the end-to-end version against a real DB)."""

from datetime import UTC, datetime
from uuid import uuid4

from app.modules.api_clients.domain.entities import ApiKey
from app.modules.api_clients.infrastructure.repository import ApiKeyRepository


def test_to_model_forwards_actor_and_soft_delete_columns() -> None:
    created_by, updated_by, deleted_by = uuid4(), uuid4(), uuid4()
    deleted_at = datetime.now(UTC)
    entity = ApiKey(
        tenant_id=uuid4(),
        name="Test Key",
        key_prefix="AbCdEfGh",
        key_hash="hash",
        scopes=["posts:read"],
        created_by=created_by,
        updated_by=updated_by,
        deleted_by=deleted_by,
        deleted_at=deleted_at,
    )

    model = ApiKeyRepository.__new__(ApiKeyRepository)._to_model(entity)

    assert model.created_by == created_by
    assert model.updated_by == updated_by
    assert model.deleted_by == deleted_by
    assert model.deleted_at == deleted_at
