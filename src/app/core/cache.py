"""Redis client + a thin cache-aside helper.

Anything module-specific (cache key shape, TTL policy) belongs in that
module's infrastructure layer; this file only wires the connection.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import TypeVar

from redis.asyncio import Connection, ConnectionPool, Redis

from app.core.config import Settings, get_settings

T = TypeVar("T")

_pool: ConnectionPool[Connection] | None = None


def get_redis_pool(settings: Settings | None = None) -> ConnectionPool[Connection]:
    global _pool
    if _pool is None:
        resolved = settings or get_settings()
        # Bounded waits: with none, a black-holed Redis (packets dropped, no refusal) would hang
        # every request that touches it instead of failing fast — which is what makes the
        # "fail open" paths (rate limiter, session denylist) and the 503 mapping possible.
        _pool = ConnectionPool.from_url(
            resolved.redis_url,
            decode_responses=True,
            socket_connect_timeout=resolved.redis_socket_timeout_seconds,
            socket_timeout=resolved.redis_socket_timeout_seconds,
        )
    return _pool


def get_redis() -> Redis:  # type: ignore[type-arg]
    return Redis(connection_pool=get_redis_pool())


class CacheAside:
    """Serialize-to-JSON cache-aside wrapper used by repositories/services.

    Unlike `BaseRepository`, this does NOT bake tenant scoping into keys
    for you — `namespace`/`key` are whatever the caller passes. If you
    cache a tenant-owned read (e.g. a role/permission lookup), the caller
    MUST include `tenant_id` in the `key` itself (e.g.
    `f"{tenant_id}:{role_id}"`), or one tenant's cached value can be served
    to another. There's no structural guard here the way `_apply_scopes`
    gives every DB query — this is a deliberately thin wrapper.
    """

    def __init__(
        self,
        redis: Redis,  # type: ignore[type-arg]
        *,
        namespace: str,
        default_ttl_seconds: int = 300,
    ) -> None:
        self._redis = redis
        self._namespace = namespace
        self._default_ttl = default_ttl_seconds

    def _key(self, key: str) -> str:
        return f"{self._namespace}:{key}"

    async def get_or_set(
        self,
        key: str,
        loader: Callable[[], Awaitable[T]],
        *,
        ttl_seconds: int | None = None,
    ) -> T:
        cache_key = self._key(key)
        cached = await self._redis.get(cache_key)
        if cached is not None:
            return json.loads(cached)  # type: ignore[no-any-return]

        value = await loader()
        await self._redis.set(
            cache_key, json.dumps(value, default=str), ex=ttl_seconds or self._default_ttl
        )
        return value

    async def invalidate(self, key: str) -> None:
        await self._redis.delete(self._key(key))

    async def invalidate_prefix(self, prefix: str) -> None:
        pattern = self._key(f"{prefix}*")
        async for k in self._redis.scan_iter(match=pattern):
            await self._redis.delete(k)
