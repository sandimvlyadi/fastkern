import pytest
from redis.asyncio import Redis

from app.core import cache
from app.core.cache import CacheAside, get_redis, get_redis_pool
from app.core.config import get_settings
from tests.fakes import FakeAsyncRedis


async def test_get_or_set_miss_calls_loader_and_caches() -> None:
    redis = FakeAsyncRedis()
    aside = CacheAside(redis, namespace="ns")
    calls = 0

    async def loader() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"value": 1}

    result = await aside.get_or_set("key", loader)
    assert result == {"value": 1}
    assert calls == 1


async def test_get_or_set_hit_skips_loader() -> None:
    redis = FakeAsyncRedis()
    aside = CacheAside(redis, namespace="ns")
    calls = 0

    async def loader() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"value": calls}

    await aside.get_or_set("key", loader)
    result = await aside.get_or_set("key", loader)
    assert result == {"value": 1}
    assert calls == 1


async def test_get_or_set_uses_custom_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = FakeAsyncRedis()
    recorded_ttls: list[int | None] = []
    original_set = redis.set

    async def spy_set(key: str, value: str, *, ex: int | None = None) -> None:
        recorded_ttls.append(ex)
        await original_set(key, value, ex=ex)

    monkeypatch.setattr(redis, "set", spy_set)
    aside = CacheAside(redis, namespace="ns", default_ttl_seconds=300)

    await aside.get_or_set("key", lambda: _const(1), ttl_seconds=60)
    assert recorded_ttls == [60]


async def _const(value: int) -> int:
    return value


async def test_invalidate_removes_key() -> None:
    redis = FakeAsyncRedis()
    aside = CacheAside(redis, namespace="ns")
    await aside.get_or_set("key", lambda: _const(1))
    await aside.invalidate("key")
    assert await redis.get("ns:key") is None


async def test_invalidate_prefix_removes_matching_keys_only() -> None:
    redis = FakeAsyncRedis()
    aside = CacheAside(redis, namespace="ns")
    redis._store["ns:userlist:1"] = "a"
    redis._store["ns:userlist:2"] = "b"
    redis._store["ns:other"] = "c"

    await aside.invalidate_prefix("userlist")

    assert "ns:userlist:1" not in redis._store
    assert "ns:userlist:2" not in redis._store
    assert "ns:other" in redis._store


def test_get_redis_pool_returns_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache, "_pool", None)
    first = get_redis_pool(get_settings())
    second = get_redis_pool(get_settings())
    assert first is second


def test_get_redis_returns_redis_instance() -> None:
    redis = get_redis()
    assert isinstance(redis, Redis)
