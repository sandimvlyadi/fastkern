from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.core.pagination import OffsetParams
from app.modules.api_clients.application.dto import CreateApiKeyInput
from app.modules.api_clients.application.use_cases import (
    CreateApiKeyUseCase,
    ListApiKeyScopesUseCase,
    ListApiKeysUseCase,
    RevokeApiKeyUseCase,
    RotateApiKeyUseCase,
    VerifyApiKeyUseCase,
)
from app.modules.api_clients.domain.entities import ApiKey
from app.modules.api_clients.domain.exceptions import (
    ApiClientNotFoundError,
    ApiKeyAlreadyExpiredError,
    ApiKeyAlreadyRevokedError,
    ApiKeyExpiredError,
    InvalidApiKeyError,
    ScopeEscalationError,
    UnknownScopeError,
)
from app.modules.api_clients.domain.hashing import API_KEY_MARKER
from app.modules.api_clients.domain.scopes import API_KEY_SCOPE_CATALOG
from app.modules.roles.domain.entities import Role
from tests.fakes import InMemoryApiKeyRepository, InMemoryUserRoleRepository


@pytest.fixture
def repo() -> InMemoryApiKeyRepository:
    return InMemoryApiKeyRepository()


async def _caller(*permissions: str) -> tuple[InMemoryUserRoleRepository, UUID]:
    """A caller holding exactly `permissions`, through one role."""
    user_roles = InMemoryUserRoleRepository()
    role = Role(tenant_id=uuid4(), name=f"caller-{uuid4().hex[:6]}", permissions=list(permissions))
    user_roles.seed_role(role)
    caller_id = uuid4()
    await user_roles.assign(caller_id, role.id)
    return user_roles, caller_id


async def _create(
    repo: InMemoryApiKeyRepository,
    *,
    scopes: list[str] | None = None,
    expires_at: datetime | None = None,
    tenant_id: UUID | None = None,
) -> tuple[ApiKey, str]:
    """Creates a key as a `*` caller, who may grant any scope."""
    user_roles, caller_id = await _caller("*")
    return await CreateApiKeyUseCase(repo, user_roles).execute(
        CreateApiKeyInput(
            tenant_id=tenant_id or uuid4(),
            name="Acme",
            scopes=["posts:read"] if scopes is None else scopes,
            expires_at=expires_at,
        ),
        caller_id=caller_id,
    )


async def _rotate(repo: InMemoryApiKeyRepository, api_key_id: UUID) -> tuple[ApiKey, str]:
    user_roles, caller_id = await _caller("*")
    return await RotateApiKeyUseCase(repo, user_roles).execute(
        api_key_id, caller_id=caller_id, overlap_hours=48
    )


async def test_create_returns_plaintext_once_and_never_again(
    repo: InMemoryApiKeyRepository,
) -> None:
    api_key, plaintext = await _create(repo)
    assert plaintext.startswith(API_KEY_MARKER)
    assert api_key.key_prefix == plaintext[: len(api_key.key_prefix)]
    assert api_key.key_hash != plaintext
    assert plaintext not in api_key.audit_snapshot().values()


async def test_create_refuses_a_scope_the_caller_does_not_hold_and_stores_nothing(
    repo: InMemoryApiKeyRepository,
) -> None:
    user_roles, caller_id = await _caller("api_clients:read", "api_clients:write", "posts:read")

    with pytest.raises(ScopeEscalationError) as exc_info:
        await CreateApiKeyUseCase(repo, user_roles).execute(
            CreateApiKeyInput(
                tenant_id=uuid4(), name="Acme", scopes=["posts:read", "files:read", "users:read"]
            ),
            caller_id=caller_id,
        )

    assert exc_info.value.context == {"scopes": "files:read, users:read"}
    assert repo.keys == {}


async def test_create_allows_scopes_the_caller_holds(repo: InMemoryApiKeyRepository) -> None:
    user_roles, caller_id = await _caller("api_clients:write", "users:read")

    api_key, _ = await CreateApiKeyUseCase(repo, user_roles).execute(
        CreateApiKeyInput(tenant_id=uuid4(), name="Acme", scopes=["users:read"]),
        caller_id=caller_id,
    )

    assert api_key.scopes == ["users:read"]


async def test_verify_accepts_the_current_key_without_writing(
    repo: InMemoryApiKeyRepository,
) -> None:
    """Verification is read-only: `last_used_at` is recorded by the request-log task, so the
    request never takes a row lock on the key."""
    _, plaintext = await _create(repo)

    verified = await VerifyApiKeyUseCase(repo).execute(plaintext)

    assert verified.last_used_at is None


async def test_verify_rejects_an_unknown_key(repo: InMemoryApiKeyRepository) -> None:
    with pytest.raises(InvalidApiKeyError):
        await VerifyApiKeyUseCase(repo).execute("not-a-real-key")


async def test_rotate_keeps_the_old_key_valid_within_the_overlap_window(
    repo: InMemoryApiKeyRepository,
) -> None:
    api_key, old_plaintext = await _create(repo)
    _, new_plaintext = await _rotate(repo, api_key.id)

    old_verified = await VerifyApiKeyUseCase(repo).execute(old_plaintext)
    new_verified = await VerifyApiKeyUseCase(repo).execute(new_plaintext)
    assert old_verified.id == api_key.id
    assert new_verified.id == api_key.id


async def test_rotate_unknown_key_raises_not_found(repo: InMemoryApiKeyRepository) -> None:
    with pytest.raises(ApiClientNotFoundError):
        await _rotate(repo, uuid4())


async def test_rotate_refuses_a_key_with_scopes_the_caller_does_not_hold(
    repo: InMemoryApiKeyRepository,
) -> None:
    """Rotating returns a working plaintext for every scope on the key, so a caller with only
    `api_clients:write` must not be able to take over a broader key an admin created."""
    api_key, _ = await _create(repo, scopes=["files:read"])
    old_hash = api_key.key_hash
    user_roles, caller_id = await _caller("api_clients:write")

    with pytest.raises(ScopeEscalationError):
        await RotateApiKeyUseCase(repo, user_roles).execute(
            api_key.id, caller_id=caller_id, overlap_hours=48
        )

    stored = await repo.get_by_id(api_key.id)
    assert stored is not None
    assert stored.key_hash == old_hash


async def test_rotate_refuses_a_revoked_key(repo: InMemoryApiKeyRepository) -> None:
    api_key, _ = await _create(repo)
    await RevokeApiKeyUseCase(repo).execute(api_key.id)

    with pytest.raises(ApiKeyAlreadyRevokedError):
        await _rotate(repo, api_key.id)


async def test_rotate_refuses_an_expired_key(repo: InMemoryApiKeyRepository) -> None:
    """Rotation keeps `expires_at`, so the new plaintext would be refused on first use."""
    api_key, _ = await _create(repo, expires_at=datetime.now(UTC) - timedelta(minutes=1))

    with pytest.raises(ApiKeyAlreadyExpiredError):
        await _rotate(repo, api_key.id)


async def test_revoke_invalidates_both_current_and_previous_hash(
    repo: InMemoryApiKeyRepository,
) -> None:
    api_key, old_plaintext = await _create(repo)
    _, new_plaintext = await _rotate(repo, api_key.id)
    await RevokeApiKeyUseCase(repo).execute(api_key.id)

    with pytest.raises(InvalidApiKeyError):
        await VerifyApiKeyUseCase(repo).execute(new_plaintext)
    with pytest.raises(InvalidApiKeyError):
        await VerifyApiKeyUseCase(repo).execute(old_plaintext)


async def test_revoke_unknown_key_raises_not_found(repo: InMemoryApiKeyRepository) -> None:
    with pytest.raises(ApiClientNotFoundError):
        await RevokeApiKeyUseCase(repo).execute(uuid4())


async def test_verify_rejects_an_expired_key(repo: InMemoryApiKeyRepository) -> None:
    _, plaintext = await _create(repo, expires_at=datetime.now(UTC) - timedelta(days=1))
    with pytest.raises(ApiKeyExpiredError):
        await VerifyApiKeyUseCase(repo).execute(plaintext)


async def test_verify_rejects_the_previous_key_once_the_key_itself_has_expired(
    repo: InMemoryApiKeyRepository,
) -> None:
    """The rotation overlap window never outlives the key's own `expires_at`."""
    api_key, old_plaintext = await _create(repo, expires_at=datetime.now(UTC) + timedelta(days=1))
    await _rotate(repo, api_key.id)
    stored = await repo.get_by_id(api_key.id)
    assert stored is not None
    stored.expires_at = datetime.now(UTC) - timedelta(minutes=1)  # expired, overlap still open

    with pytest.raises(ApiKeyExpiredError):
        await VerifyApiKeyUseCase(repo).execute(old_plaintext)


async def test_verify_rejects_a_key_whose_status_was_flipped_without_clearing_its_hash(
    repo: InMemoryApiKeyRepository,
) -> None:
    """The real `RevokeApiKeyUseCase` always clears `key_hash` too (so a revoked key never
    matches at all), but a row whose status changed some other way must still be refused."""
    api_key, plaintext = await _create(repo)
    stored = await repo.get_by_id(api_key.id)
    assert stored is not None
    stored.status = "revoked"

    with pytest.raises(InvalidApiKeyError):
        await VerifyApiKeyUseCase(repo).execute(plaintext)


async def test_verify_rejects_a_key_two_rotations_stale(repo: InMemoryApiKeyRepository) -> None:
    """The overlap window only ever covers the immediately-previous key — a plaintext from two
    rotations ago matches neither the current nor the previous hash."""
    api_key, first_plaintext = await _create(repo)
    await _rotate(repo, api_key.id)
    await _rotate(repo, api_key.id)

    with pytest.raises(InvalidApiKeyError):
        await VerifyApiKeyUseCase(repo).execute(first_plaintext)


async def test_list_paginates(repo: InMemoryApiKeyRepository) -> None:
    tenant_id = uuid4()
    for _ in range(3):
        await _create(repo, scopes=[], tenant_id=tenant_id)
    page = await ListApiKeysUseCase(repo).execute(OffsetParams(page=1, page_size=2))
    assert page.total == 3
    assert len(page.items) == 2


async def test_create_with_unknown_scope_raises_and_stores_nothing(
    repo: InMemoryApiKeyRepository,
) -> None:
    with pytest.raises(UnknownScopeError):
        await _create(repo, scopes=["posts:read", "orders:read"])
    assert (await ListApiKeysUseCase(repo).execute(OffsetParams())).total == 0


async def test_list_scopes_returns_the_catalog_flagged_for_the_caller() -> None:
    user_roles, caller_id = await _caller("posts:read", "users:read")

    page = await ListApiKeyScopesUseCase(user_roles).execute(caller_id, OffsetParams())

    assert [s.code for s in page.items] == [s.code for s in API_KEY_SCOPE_CATALOG]
    assert page.total == len(API_KEY_SCOPE_CATALOG)
    assert {s.code for s in page.items if s.grantable} == {"posts:read", "users:read"}


async def test_list_scopes_marks_everything_grantable_for_a_wildcard_caller() -> None:
    user_roles, caller_id = await _caller("*")

    page = await ListApiKeyScopesUseCase(user_roles).execute(caller_id, OffsetParams())

    assert all(s.grantable for s in page.items)
