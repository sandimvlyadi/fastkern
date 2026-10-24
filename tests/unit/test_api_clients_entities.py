from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.modules.api_clients.domain.entities import ApiKey


def _api_key(**overrides: object) -> ApiKey:
    defaults: dict[str, object] = {
        "tenant_id": uuid4(),
        "name": "Test Key",
        "key_prefix": "AbCdEfGh",
        "key_hash": "hash",
        "scopes": ["posts:read"],
    }
    defaults.update(overrides)
    return ApiKey(**defaults)  # type: ignore[arg-type]


def test_has_scope_exact_match() -> None:
    assert _api_key(scopes=["posts:read"]).has_scope("posts:read")


def test_has_scope_wildcard() -> None:
    assert _api_key(scopes=["*"]).has_scope("anything:anything")


def test_has_scope_denies_unlisted() -> None:
    assert not _api_key(scopes=["posts:read"]).has_scope("posts:write")


def test_has_scope_empty_denies_everything() -> None:
    assert not _api_key(scopes=[]).has_scope("posts:read")


def test_is_usable_active_no_expiry() -> None:
    assert _api_key(status="active", expires_at=None).is_usable(now=datetime.now(UTC))


def test_is_usable_active_not_yet_expired() -> None:
    key = _api_key(status="active", expires_at=datetime.now(UTC) + timedelta(hours=1))
    assert key.is_usable(now=datetime.now(UTC))


def test_is_usable_active_but_expired() -> None:
    key = _api_key(status="active", expires_at=datetime.now(UTC) - timedelta(hours=1))
    assert not key.is_usable(now=datetime.now(UTC))


def test_is_usable_revoked() -> None:
    assert not _api_key(status="revoked").is_usable(now=datetime.now(UTC))


def test_previous_key_usable_within_window() -> None:
    key = _api_key(
        previous_key_hash="old-hash", previous_key_expires_at=datetime.now(UTC) + timedelta(hours=1)
    )
    assert key.previous_key_usable(now=datetime.now(UTC))


def test_previous_key_usable_past_window() -> None:
    key = _api_key(
        previous_key_hash="old-hash", previous_key_expires_at=datetime.now(UTC) - timedelta(hours=1)
    )
    assert not key.previous_key_usable(now=datetime.now(UTC))


def test_previous_key_usable_when_revoked() -> None:
    key = _api_key(
        status="revoked",
        previous_key_hash="old-hash",
        previous_key_expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    assert not key.previous_key_usable(now=datetime.now(UTC))


def test_audit_snapshot_never_includes_hashes() -> None:
    snapshot = _api_key(
        key_hash="secret-hash", previous_key_hash="old-secret-hash"
    ).audit_snapshot()
    assert "key_hash" not in snapshot
    assert "keyHash" not in snapshot
    assert "secret-hash" not in snapshot.values()


def test_previous_key_is_not_usable_once_the_key_itself_has_expired() -> None:
    now = datetime.now(UTC)
    key = _api_key(
        previous_key_hash="old-hash",
        previous_key_expires_at=now + timedelta(hours=24),
        expires_at=now - timedelta(minutes=1),
    )
    assert key.is_expired(now=now)
    assert not key.previous_key_usable(now=now)
