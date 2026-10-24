"""The Redis session denylist itself, and its fail-open behaviour."""

from typing import Any
from uuid import uuid4

from httpx import AsyncClient
from redis import exceptions as redis_exc

from app.core.di import get_redis_client
from app.main import app as real_app
from app.modules.auth.infrastructure.token_session_store import (
    SESSION_DENYLIST_BACKEND_ERRORS,
    RedisTokenSessionStore,
)
from tests.helpers import auth_header, register_and_login


def _store() -> RedisTokenSessionStore:
    return RedisTokenSessionStore(get_redis_client())


async def test_revoked_session_is_reported_revoked_and_others_are_not() -> None:
    store = _store()
    revoked, untouched = str(uuid4()), str(uuid4())

    await store.revoke_session(session_id=revoked, ttl_seconds=60)

    assert await store.is_session_revoked(session_id=revoked) is True
    assert await store.is_session_revoked(session_id=untouched) is False


async def test_revocation_expires_with_its_ttl_and_never_stores_a_non_positive_one() -> None:
    store = _store()
    session_id = str(uuid4())

    await store.revoke_session(session_id=session_id, ttl_seconds=0)  # clamped to 1s

    redis = get_redis_client()
    ttl = await redis.ttl(f"revoked_sid:{session_id}")
    assert 0 < ttl <= 1
    await redis.delete(f"revoked_sid:{session_id}")


class _BrokenRedis:
    """Every command fails like a Redis that is down."""

    async def exists(self, *_: Any, **__: Any) -> int:
        raise redis_exc.ConnectionError("Error 111 connecting to redis:6379. Connection refused.")


class _TimingOutRedis:
    async def exists(self, *_: Any, **__: Any) -> int:
        raise redis_exc.TimeoutError("Timeout reading from socket")


class _OSErrorRedis:
    async def exists(self, *_: Any, **__: Any) -> int:
        raise ConnectionRefusedError("[Errno 111] Connect call failed")


async def test_denylist_fails_open_and_counts_when_redis_is_unreachable() -> None:
    for broken in (_BrokenRedis(), _TimingOutRedis(), _OSErrorRedis()):
        store = RedisTokenSessionStore(broken)  # type: ignore[arg-type]
        before = SESSION_DENYLIST_BACKEND_ERRORS._value.get()

        revoked = await store.is_session_revoked(session_id=str(uuid4()))

        assert revoked is False  # fail open: not "revoked", not an exception
        assert SESSION_DENYLIST_BACKEND_ERRORS._value.get() == before + 1


async def test_authenticated_requests_still_work_while_the_denylist_is_down(
    client: AsyncClient, tenant_id: str
) -> None:
    _, access, _ = await register_and_login(client, tenant_id, "failopen@example.com")
    before = SESSION_DENYLIST_BACKEND_ERRORS._value.get()

    real_app.dependency_overrides[get_redis_client] = lambda: _BrokenRedis()
    try:
        response = await client.get("/api/v1/auth/me", headers=auth_header(access))
    finally:
        real_app.dependency_overrides.pop(get_redis_client, None)

    assert response.status_code == 200  # not a 500/503: the API stays up
    assert SESSION_DENYLIST_BACKEND_ERRORS._value.get() > before
