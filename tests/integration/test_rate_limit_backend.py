"""The parts of rate limiting the in-memory tests can't show: the real
`limits.aio` Redis storage (`implementation="redispy"`), and an unreachable
Redis. The first skips itself when no Redis is running."""

import time
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from app.core.config import Settings, get_settings
from app.core.rate_limit import RateLimitTier, build_rate_limiter

# Dedicated DB so these keys never mix with the app's (0), Celery's (1, 2)...
_TEST_DB = "/15"
# Non-routable address: packets are dropped, like a firewalled / hung Redis.
_BLACK_HOLE = "redis://10.255.255.1:6379/15"


def _settings(redis_url: str, **overrides: object) -> Settings:
    return Settings(  # type: ignore[arg-type]
        secret_key="x" * 32,
        database_url="sqlite+aiosqlite:///./unit-test.db",
        redis_url=redis_url,
        rate_limit_enabled=True,
        **overrides,
    )


@pytest.fixture
async def real_redis_url() -> str:
    parts = urlsplit(get_settings().redis_url)
    url = urlunsplit(parts._replace(path=_TEST_DB))
    client = Redis.from_url(url, socket_connect_timeout=0.5)
    try:
        await client.ping()
    except Exception:
        pytest.skip("no Redis reachable at REDIS_URL")
    finally:
        await client.aclose()
    return url


async def test_limits_are_enforced_by_a_real_redis(real_redis_url: str) -> None:
    limiter = build_rate_limiter(_settings(real_redis_url, rate_limit_default="2/minute"))
    who = uuid4().hex  # unique bucket; the key expires with its 60s window

    decisions = [await limiter.check(RateLimitTier.DEFAULT, "test", who) for _ in range(3)]

    assert [d.allowed for d in decisions if d] == [True, True, False]
    assert [d.remaining for d in decisions if d] == [1, 0, 0]
    assert decisions[2] is not None
    assert 1 <= decisions[2].retry_after <= 60
    assert decisions[0] is not None
    assert decisions[0].reset_at >= int(time.time())


async def test_ip_tier_checks_against_a_real_redis_without_a_stats_round_trip(
    real_redis_url: str,
) -> None:
    limiter = build_rate_limiter(_settings(real_redis_url, rate_limit_ip="1/minute"))
    ip = f"203.0.113.{uuid4().int % 250}-{uuid4().hex}"  # unique key, TEST-NET-3 style

    first = await limiter.check(RateLimitTier.IP, ip, report_allowed=False)
    second = await limiter.check(RateLimitTier.IP, ip, report_allowed=False)

    assert first is None
    assert second is not None and not second.allowed


async def test_a_black_holed_redis_costs_one_timeout_not_one_per_request() -> None:
    limiter = build_rate_limiter(_settings(_BLACK_HOLE))

    started = time.perf_counter()
    results = [
        await limiter.check(tier, "test", "user")
        for _ in range(10)
        for tier in (RateLimitTier.IP, RateLimitTier.DEFAULT, RateLimitTier.WRITE)
    ]
    elapsed = time.perf_counter() - started

    assert results == [None] * 30  # fail-open every single time
    # 30 checks without the breaker would each wait out the 250ms timeout (~7.5s).
    # With it, only the first one does (a resolver error on this address is just as fast).
    assert elapsed < 2.0, f"30 checks took {elapsed:.2f}s"
