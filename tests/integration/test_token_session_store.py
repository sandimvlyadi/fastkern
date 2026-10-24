from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.core.di import get_redis_client
from app.modules.auth.infrastructure.token_session_store import RedisTokenSessionStore


def _store() -> RedisTokenSessionStore:
    return RedisTokenSessionStore(get_redis_client())


async def test_store_then_is_valid() -> None:
    store = _store()
    user_id = uuid4()
    await store.store_refresh_token(
        user_id=user_id, jti="jti-1", expires_at=datetime.now(UTC) + timedelta(minutes=5)
    )
    assert await store.is_refresh_token_valid(user_id=user_id, jti="jti-1") is True


async def test_revoke_makes_token_invalid() -> None:
    store = _store()
    user_id = uuid4()
    await store.store_refresh_token(
        user_id=user_id, jti="jti-1", expires_at=datetime.now(UTC) + timedelta(minutes=5)
    )
    await store.revoke_refresh_token(user_id=user_id, jti="jti-1")
    assert await store.is_refresh_token_valid(user_id=user_id, jti="jti-1") is False


async def test_revoke_all_for_user_removes_every_jti() -> None:
    store = _store()
    user_id = uuid4()
    expires_at = datetime.now(UTC) + timedelta(minutes=5)
    await store.store_refresh_token(user_id=user_id, jti="jti-1", expires_at=expires_at)
    await store.store_refresh_token(user_id=user_id, jti="jti-2", expires_at=expires_at)

    await store.revoke_all_for_user(user_id=user_id)

    assert await store.is_refresh_token_valid(user_id=user_id, jti="jti-1") is False
    assert await store.is_refresh_token_valid(user_id=user_id, jti="jti-2") is False


async def test_expired_at_in_the_past_still_stores_with_min_ttl() -> None:
    store = _store()
    user_id = uuid4()
    await store.store_refresh_token(
        user_id=user_id, jti="jti-past", expires_at=datetime.now(UTC) - timedelta(minutes=5)
    )
    # TTL is clamped to at least 1 second rather than a negative/zero value,
    # so the key must still exist immediately after being stored.
    assert await store.is_refresh_token_valid(user_id=user_id, jti="jti-past") is True
