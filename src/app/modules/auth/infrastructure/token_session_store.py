"""Redis-backed refresh-token session store.

Each valid refresh token's `jti` is stored as a key so it can be revoked
individually (logout) or in bulk (revoke-all-for-user, e.g. on password
reset) — a stateless JWT alone can't support revocation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from prometheus_client import Counter
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.logging import get_logger
from app.modules.auth.domain.ports import TokenSessionStorePort

logger = get_logger(__name__)

SESSION_DENYLIST_BACKEND_ERRORS = Counter(
    "session_denylist_backend_errors",
    "Session-denylist lookups that failed because Redis was unreachable (the check fails open).",
)


class RedisTokenSessionStore(TokenSessionStorePort):
    def __init__(self, redis: Redis) -> None:  # type: ignore[type-arg]
        self._redis = redis

    def _key(self, user_id: UUID, jti: str) -> str:
        return f"refresh_token:{user_id}:{jti}"

    def _user_index_key(self, user_id: UUID) -> str:
        return f"refresh_token_index:{user_id}"

    async def store_refresh_token(self, *, user_id: UUID, jti: str, expires_at: datetime) -> None:
        ttl = max(int((expires_at - datetime.now(UTC)).total_seconds()), 1)
        key = self._key(user_id, jti)
        index_key = self._user_index_key(user_id)
        # Pipelined into one round trip instead of three sequential
        # awaits — this runs on every login/register/refresh/switch-role.
        async with self._redis.pipeline(transaction=False) as pipe:
            pipe.set(key, "1", ex=ttl)
            pipe.sadd(index_key, jti)
            pipe.expire(index_key, ttl)
            await pipe.execute()

    async def is_refresh_token_valid(self, *, user_id: UUID, jti: str) -> bool:
        return bool(await self._redis.exists(self._key(user_id, jti)))

    async def revoke_refresh_token(self, *, user_id: UUID, jti: str) -> None:
        await self._redis.delete(self._key(user_id, jti))
        await self._redis.srem(self._user_index_key(user_id), jti)

    async def consume_refresh_token(self, *, user_id: UUID, jti: str) -> bool:
        # GETDEL is atomic (single Redis command), so two concurrent
        # callers presenting the same jti can't both see it as valid —
        # only the first GETDEL returns the value; the second sees None.
        deleted = await self._redis.getdel(self._key(user_id, jti))
        if deleted is None:
            return False
        await self._redis.srem(self._user_index_key(user_id), jti)
        return True

    @staticmethod
    def _revoked_session_key(session_id: str) -> str:
        return f"revoked_sid:{session_id}"

    async def revoke_session(self, *, session_id: str, ttl_seconds: int) -> None:
        await self._redis.set(self._revoked_session_key(session_id), "1", ex=max(ttl_seconds, 1))

    async def is_session_revoked(self, *, session_id: str) -> bool:
        try:
            return bool(await self._redis.exists(self._revoked_session_key(session_id)))
        except (RedisError, OSError) as exc:
            # Fail OPEN, like the rate limiter: a Redis outage must not turn every authenticated
            # request into an error. The cost is that a logged-out access token stays usable
            # until it expires (<= ACCESS_TOKEN_EXPIRE_MINUTES).
            SESSION_DENYLIST_BACKEND_ERRORS.inc()
            logger.warning("session_denylist_unavailable", error_type=type(exc).__name__)
            return False

    async def revoke_all_for_user(self, *, user_id: UUID) -> None:
        index_key = self._user_index_key(user_id)
        jtis = await self._redis.smembers(index_key)
        if jtis:
            # One DEL for every session key instead of N sequential round
            # trips — same data, a fraction of the network latency.
            await self._redis.delete(*(self._key(user_id, jti) for jti in jtis))
        await self._redis.delete(index_key)
