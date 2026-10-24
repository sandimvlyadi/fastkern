import asyncio
import time
from typing import Any
from uuid import uuid4

import pytest
from limits import parse
from limits.aio.storage import MemoryStorage
from limits.util import WindowStats
from prometheus_client import REGISTRY
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import Response

import app.core.rate_limit as rate_limit_module
from app.core.config import Settings
from app.core.context import update_request_context
from app.core.exceptions import RateLimitedError
from app.core.rate_limit import (
    LIMIT_HEADER,
    REMAINING_HEADER,
    RESET_HEADER,
    RETRY_AFTER_HEADER,
    RateLimitDecision,
    RateLimiter,
    RateLimitTier,
    _apply_headers,
    build_rate_limiter,
    get_rate_limiter,
    rate_limit,
)


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "secret_key": "x" * 32,
        "database_url": "sqlite+aiosqlite:///./unit-test.db",
        "rate_limit_enabled": True,
        "rate_limit_default": "3/minute",
        "rate_limit_auth": "2/minute",
        "rate_limit_write": "1/minute",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def _limiter(**overrides: object) -> RateLimiter:
    return build_rate_limiter(_settings(**overrides), storage=MemoryStorage())


def _request(*, client: tuple[str, int] | None = ("10.0.0.1", 12345)) -> Request:
    scope: dict[str, object] = {"type": "http", "method": "GET", "path": "/", "headers": []}
    if client is not None:
        scope["client"] = client
    return Request(scope)


# --- config -----------------------------------------------------------------


@pytest.mark.parametrize("field", ["rate_limit_ip", "rate_limit_default", "rate_limit_auth"])
def test_malformed_rate_limit_fails_fast_at_startup(field: str) -> None:
    with pytest.raises(ValidationError):
        _settings(**{field: "not-a-limit"})


@pytest.mark.parametrize("field", ["rate_limit_ip", "rate_limit_default", "rate_limit_auth"])
@pytest.mark.parametrize("value", ["0/minute", "0/second"])
def test_a_limit_that_rejects_every_request_is_refused(field: str, value: str) -> None:
    # "0/minute" parses fine, but would lock the whole API out.
    with pytest.raises(ValidationError, match="at least 1 request"):
        _settings(**{field: value})


def test_rate_limit_is_enabled_and_has_sane_defaults() -> None:
    settings = Settings(secret_key="x" * 32, database_url="sqlite+aiosqlite:///./x.db")
    # pytest env sets RATE_LIMIT_ENABLED=false, so only check the tiers here.
    assert settings.rate_limit_ip == "300/minute"
    assert settings.rate_limit_default == "100/minute"
    assert settings.rate_limit_auth == "10/minute"
    assert settings.rate_limit_write == "30/minute"


# --- RateLimiter --------------------------------------------------------------


async def test_check_allows_up_to_the_limit_then_denies() -> None:
    limiter = _limiter()

    decisions = [await limiter.check(RateLimitTier.DEFAULT, "t", "u") for _ in range(4)]

    assert [d.allowed for d in decisions if d] == [True, True, True, False]
    assert [d.remaining for d in decisions if d] == [2, 1, 0, 0]
    assert decisions[0] is not None
    assert decisions[0].limit == 3


async def test_buckets_are_isolated_per_identifier_and_tier() -> None:
    limiter = _limiter(rate_limit_default="1/minute", rate_limit_write="1/minute")

    first = await limiter.check(RateLimitTier.DEFAULT, "tenant-a", "user-1")
    other_user = await limiter.check(RateLimitTier.DEFAULT, "tenant-a", "user-2")
    other_tenant = await limiter.check(RateLimitTier.DEFAULT, "tenant-b", "user-1")
    other_tier = await limiter.check(RateLimitTier.WRITE, "tenant-a", "user-1")
    repeated = await limiter.check(RateLimitTier.DEFAULT, "tenant-a", "user-1")

    assert first and first.allowed
    assert other_user and other_user.allowed
    assert other_tenant and other_tenant.allowed
    assert other_tier and other_tier.allowed
    assert repeated and not repeated.allowed


async def test_disabled_limiter_never_limits() -> None:
    limiter = _limiter(rate_limit_enabled=False)

    assert limiter.enabled is False
    assert await limiter.check(RateLimitTier.AUTH, "t", "u") is None


async def test_limiter_without_storage_is_disabled() -> None:
    limiter = RateLimiter(limits={}, storage=None, enabled=True)

    assert limiter.enabled is False
    assert await limiter.check(RateLimitTier.AUTH, "t", "u") is None


async def test_backend_failure_fails_open_and_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    limiter = _limiter()

    async def _boom(*_: object, **__: object) -> bool:
        raise ConnectionError("redis is down")

    monkeypatch.setattr(limiter._strategy, "hit", _boom)

    assert await limiter.check(RateLimitTier.DEFAULT, "t", "u") is None


def test_build_rate_limiter_uses_redis_storage_when_enabled_and_none_given() -> None:
    limiter = build_rate_limiter(_settings(redis_url="redis://localhost:6379/5"))

    assert limiter.enabled is True  # constructing the storage must not connect


def test_get_rate_limiter_is_cached_and_honours_the_env_flag() -> None:
    get_rate_limiter.cache_clear()
    try:
        limiter = get_rate_limiter()
        assert get_rate_limiter() is limiter
        assert limiter.enabled is False  # pytest env: RATE_LIMIT_ENABLED=false
    finally:
        get_rate_limiter.cache_clear()


# --- decision / headers -------------------------------------------------------


def test_denied_decision_carries_retry_after_and_allowed_one_does_not() -> None:
    allowed = RateLimitDecision(allowed=True, limit=5, remaining=4, reset_at=2_000_000_000)
    denied = RateLimitDecision(allowed=False, limit=5, remaining=0, reset_at=2_000_000_000)

    assert RETRY_AFTER_HEADER not in allowed.headers()
    assert allowed.headers()[LIMIT_HEADER] == "5"
    assert allowed.headers()[REMAINING_HEADER] == "4"
    assert allowed.headers()[RESET_HEADER] == "2000000000"
    assert int(denied.headers()[RETRY_AFTER_HEADER]) >= 1


def test_retry_after_is_at_least_one_second_even_if_window_already_reset() -> None:
    stale = RateLimitDecision(allowed=False, limit=1, remaining=0, reset_at=1)
    assert stale.retry_after == 1


def test_apply_headers_keeps_the_most_restrictive_tier() -> None:
    response = Response()
    loose = RateLimitDecision(allowed=True, limit=100, remaining=90, reset_at=10).headers()
    tight = RateLimitDecision(allowed=True, limit=30, remaining=5, reset_at=10).headers()

    _apply_headers(response, loose)
    _apply_headers(response, tight)
    assert response.headers[LIMIT_HEADER] == "30"

    _apply_headers(response, loose)  # a looser tier arriving later must not win
    assert response.headers[LIMIT_HEADER] == "30"
    assert response.headers[REMAINING_HEADER] == "5"


# --- dependency ---------------------------------------------------------------


async def test_dependency_keys_pre_auth_callers_by_ip() -> None:
    limiter = _limiter(rate_limit_auth="1/minute")
    dependency = rate_limit(RateLimitTier.AUTH)

    await dependency(_request(client=("10.0.0.1", 1)), Response(), limiter)
    await dependency(_request(client=("10.0.0.2", 1)), Response(), limiter)  # other IP: fine
    with pytest.raises(RateLimitedError) as exc_info:
        await dependency(_request(client=("10.0.0.1", 2)), Response(), limiter)

    assert exc_info.value.headers[RETRY_AFTER_HEADER]
    assert exc_info.value.context["retry_after"] >= 1


async def test_dependency_keys_authenticated_callers_by_user_not_ip() -> None:
    limiter = _limiter(rate_limit_default="1/minute")
    dependency = rate_limit(RateLimitTier.DEFAULT)
    tenant_id = uuid4()

    update_request_context(tenant_id=tenant_id, user_id=uuid4())
    await dependency(_request(), Response(), limiter)
    update_request_context(user_id=uuid4())  # same IP + tenant, different user
    await dependency(_request(), Response(), limiter)


async def test_ip_tier_keys_on_ip_alone_even_for_authenticated_callers() -> None:
    limiter = _limiter(rate_limit_ip="2/minute")
    dependency = rate_limit(RateLimitTier.IP)

    # Different users and tenants behind one IP share the (loose) IP bucket...
    update_request_context(tenant_id=uuid4(), user_id=uuid4())
    await dependency(_request(client=("10.0.0.1", 1)), Response(), limiter)
    update_request_context(tenant_id=uuid4(), user_id=uuid4())
    await dependency(_request(client=("10.0.0.1", 2)), Response(), limiter)
    with pytest.raises(RateLimitedError):
        await dependency(_request(client=("10.0.0.1", 3)), Response(), limiter)
    # ...while another IP has its own.
    await dependency(_request(client=("10.0.0.2", 1)), Response(), limiter)


async def test_ip_and_default_tiers_do_not_share_a_bucket() -> None:
    limiter = _limiter(rate_limit_ip="1/minute", rate_limit_default="1/minute")

    ip_hit = await limiter.check(RateLimitTier.IP, "10.0.0.1")
    default_hit = await limiter.check(RateLimitTier.DEFAULT, "no-tenant", "10.0.0.1")

    assert ip_hit and ip_hit.allowed
    assert default_hit and default_hit.allowed


async def test_dependency_without_client_still_works() -> None:
    limiter = _limiter()
    response = Response()

    await rate_limit()(_request(client=None), response, limiter)

    assert response.headers[REMAINING_HEADER] == "2"


async def test_dependency_is_a_no_op_when_limiting_is_disabled() -> None:
    limiter = _limiter(rate_limit_enabled=False)
    response = Response()

    await rate_limit()(_request(), response, limiter)

    assert LIMIT_HEADER not in response.headers


def test_rate_limited_error_defaults_to_no_headers() -> None:
    assert RateLimitedError().headers == {}


# --- circuit breaker ----------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _RecordingLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def warning(self, event: str, **_: Any) -> None:
        self.events.append(("warning", event))

    def info(self, event: str, **_: Any) -> None:
        self.events.append(("info", event))


def _breaker_limiter(clock: _Clock) -> RateLimiter:
    limits = {tier: parse("3/minute") for tier in RateLimitTier}
    return RateLimiter(
        limits=limits, storage=MemoryStorage(), breaker_cooldown_seconds=5.0, clock=clock
    )


def _break_backend(
    monkeypatch: pytest.MonkeyPatch, limiter: RateLimiter
) -> tuple[dict[str, int], Any]:
    """Makes every Redis call fail; returns (call counter, the real `hit`)."""
    assert limiter._strategy is not None
    calls = {"hit": 0}
    original = limiter._strategy.hit

    async def _boom(*_: object, **__: object) -> bool:
        calls["hit"] += 1
        raise ConnectionError("redis is down")

    monkeypatch.setattr(limiter._strategy, "hit", _boom)
    return calls, original


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _RecordingLogger:
    rec = _RecordingLogger()
    monkeypatch.setattr(rate_limit_module, "logger", rec)
    return rec


async def test_breaker_skips_redis_after_a_failure_and_warns_only_once(
    monkeypatch: pytest.MonkeyPatch, recorder: _RecordingLogger
) -> None:
    limiter = _breaker_limiter(_Clock())
    calls, _ = _break_backend(monkeypatch, limiter)

    results = [await limiter.check(RateLimitTier.DEFAULT, "t", "u") for _ in range(5)]

    assert results == [None] * 5  # fail-open every time...
    assert calls["hit"] == 1  # ...but Redis was only tried once
    assert recorder.events == [("warning", "rate_limit_backend_unavailable")]


async def test_breaker_skips_all_tiers_of_the_same_request_after_the_first_failure(
    monkeypatch: pytest.MonkeyPatch, recorder: _RecordingLogger
) -> None:
    limiter = _breaker_limiter(_Clock())
    calls, _ = _break_backend(monkeypatch, limiter)

    for tier in (RateLimitTier.IP, RateLimitTier.DEFAULT, RateLimitTier.WRITE):
        await limiter.check(tier, "t", "u")

    assert calls["hit"] == 1  # one timeout per outage, not one per tier


async def test_breaker_probes_after_the_cooldown_and_stays_quiet_while_still_down(
    monkeypatch: pytest.MonkeyPatch, recorder: _RecordingLogger
) -> None:
    clock = _Clock()
    limiter = _breaker_limiter(clock)
    calls, _ = _break_backend(monkeypatch, limiter)
    await limiter.check(RateLimitTier.DEFAULT, "t", "u")  # trips the breaker

    clock.now += 4.9  # still cooling down
    await limiter.check(RateLimitTier.DEFAULT, "t", "u")
    assert calls["hit"] == 1

    clock.now += 0.2  # cooldown over: one probe, which fails again
    await limiter.check(RateLimitTier.DEFAULT, "t", "u")
    assert calls["hit"] == 2
    clock.now += 1
    await limiter.check(RateLimitTier.DEFAULT, "t", "u")  # cooling down again
    assert calls["hit"] == 2
    assert recorder.events == [("warning", "rate_limit_backend_unavailable")]  # no repeat warning


async def test_breaker_closes_again_when_the_probe_succeeds(
    monkeypatch: pytest.MonkeyPatch, recorder: _RecordingLogger
) -> None:
    clock = _Clock()
    limiter = _breaker_limiter(clock)
    _, original_hit = _break_backend(monkeypatch, limiter)
    await limiter.check(RateLimitTier.DEFAULT, "t", "u")  # trips the breaker
    assert limiter._strategy is not None
    monkeypatch.setattr(limiter._strategy, "hit", original_hit)  # Redis is back

    clock.now += 6
    probe = await limiter.check(RateLimitTier.DEFAULT, "t", "u")
    follow_up = await limiter.check(RateLimitTier.DEFAULT, "t", "u")  # no cooldown any more

    assert probe and probe.allowed
    assert follow_up and follow_up.remaining == probe.remaining - 1
    assert recorder.events == [
        ("warning", "rate_limit_backend_unavailable"),
        ("info", "rate_limit_backend_recovered"),
    ]


async def test_only_one_request_probes_a_recovering_backend(
    monkeypatch: pytest.MonkeyPatch, recorder: _RecordingLogger
) -> None:
    clock = _Clock()
    limiter = _breaker_limiter(clock)
    _, original_hit = _break_backend(monkeypatch, limiter)
    await limiter.check(RateLimitTier.DEFAULT, "t", "u")  # trips the breaker
    assert limiter._strategy is not None

    gate = asyncio.Event()
    probes = 0

    async def _slow_hit(*args: Any, **kwargs: Any) -> bool:
        nonlocal probes
        probes += 1
        await gate.wait()
        return await original_hit(*args, **kwargs)  # type: ignore[no-any-return]

    monkeypatch.setattr(limiter._strategy, "hit", _slow_hit)
    clock.now += 6

    probe = asyncio.create_task(limiter.check(RateLimitTier.DEFAULT, "t", "u"))
    await asyncio.sleep(0)  # the probe is now waiting inside hit()
    assert await limiter.check(RateLimitTier.DEFAULT, "t", "other") is None  # skipped, not queued
    assert probes == 1

    gate.set()
    decision = await probe
    assert decision and decision.allowed


# --- check(): stats round trip and window rollover ----------------------------


async def test_report_allowed_false_skips_the_stats_round_trip_but_not_for_a_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limiter = _limiter(rate_limit_ip="1/minute")
    assert limiter._strategy is not None
    stats_calls = 0
    original_stats = limiter._strategy.get_window_stats

    async def _counting_stats(*args: Any, **kwargs: Any) -> WindowStats:
        nonlocal stats_calls
        stats_calls += 1
        return await original_stats(*args, **kwargs)

    monkeypatch.setattr(limiter._strategy, "get_window_stats", _counting_stats)

    allowed = await limiter.check(RateLimitTier.IP, "10.0.0.1", report_allowed=False)
    denied = await limiter.check(RateLimitTier.IP, "10.0.0.1", report_allowed=False)

    assert allowed is None
    assert stats_calls == 1  # only the denial fetched stats (it needs Retry-After)
    assert denied and not denied.allowed and denied.remaining == 0
    assert denied.retry_after >= 1


async def test_a_denial_whose_window_rolled_over_meanwhile_advises_a_one_second_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limiter = _limiter()
    assert limiter._strategy is not None

    async def _denied(*_: object, **__: object) -> bool:
        return False

    async def _fresh_window(*_: object, **__: object) -> WindowStats:
        # hit() was denied, but by the time the stats were read the window had reset.
        return WindowStats(reset_time=time.time() + 55, remaining=5)

    monkeypatch.setattr(limiter._strategy, "hit", _denied)
    monkeypatch.setattr(limiter._strategy, "get_window_stats", _fresh_window)

    decision = await limiter.check(RateLimitTier.DEFAULT, "t", "u")

    assert decision and not decision.allowed
    assert decision.remaining == 0
    assert decision.retry_after <= 2  # not the ~55s of the brand-new window


async def test_ip_tier_dependency_sends_no_headers_for_an_allowed_request() -> None:
    limiter = _limiter()
    response = Response()

    await rate_limit(RateLimitTier.IP)(_request(), response, limiter)

    assert LIMIT_HEADER not in response.headers


# --- metrics ------------------------------------------------------------------


def _degraded() -> float | None:
    return REGISTRY.get_sample_value("rate_limit_backend_degraded")


def _errors(tier: str) -> float:
    return REGISTRY.get_sample_value("rate_limit_backend_errors_total", {"tier": tier}) or 0.0


def test_metrics_are_exported_at_zero_before_anything_fails() -> None:
    # 0 has to mean healthy, or every start would look like an outage.
    assert _degraded() == 0.0
    for tier in RateLimitTier:
        assert (
            REGISTRY.get_sample_value("rate_limit_backend_errors_total", {"tier": tier.value})
            is not None
        )


async def test_a_backend_failure_flags_the_limiter_as_degraded_and_counts_the_error(
    monkeypatch: pytest.MonkeyPatch, recorder: _RecordingLogger
) -> None:
    limiter = _breaker_limiter(_Clock())
    _break_backend(monkeypatch, limiter)
    auth_before, write_before = _errors("auth"), _errors("write")

    for _ in range(4):  # only the first one reaches Redis; the rest are skipped
        await limiter.check(RateLimitTier.AUTH, "t", "u")

    assert _degraded() == 1.0
    assert _errors("auth") == auth_before + 1
    assert _errors("write") == write_before  # other tiers untouched


async def test_a_failed_probe_counts_another_error_and_stays_degraded(
    monkeypatch: pytest.MonkeyPatch, recorder: _RecordingLogger
) -> None:
    clock = _Clock()
    limiter = _breaker_limiter(clock)
    _break_backend(monkeypatch, limiter)
    before = _errors("default")
    await limiter.check(RateLimitTier.DEFAULT, "t", "u")

    clock.now += 6  # cooldown over: the probe fails too
    await limiter.check(RateLimitTier.DEFAULT, "t", "u")

    assert _degraded() == 1.0
    assert _errors("default") == before + 2


async def test_recovery_clears_the_degraded_gauge(
    monkeypatch: pytest.MonkeyPatch, recorder: _RecordingLogger
) -> None:
    clock = _Clock()
    limiter = _breaker_limiter(clock)
    _, original_hit = _break_backend(monkeypatch, limiter)
    await limiter.check(RateLimitTier.DEFAULT, "t", "u")
    assert _degraded() == 1.0
    assert limiter._strategy is not None
    monkeypatch.setattr(limiter._strategy, "hit", original_hit)

    clock.now += 6
    await limiter.check(RateLimitTier.DEFAULT, "t", "u")

    assert _degraded() == 0.0


async def test_a_healthy_limiter_never_touches_the_metrics() -> None:
    limiter = _limiter()
    before = {t.value: _errors(t.value) for t in RateLimitTier}

    for tier in RateLimitTier:
        await limiter.check(tier, "t", "u")

    assert _degraded() == 0.0
    assert {t.value: _errors(t.value) for t in RateLimitTier} == before


async def test_the_gauge_drops_back_by_itself_when_the_cooldown_ends_with_no_traffic(
    monkeypatch: pytest.MonkeyPatch, recorder: _RecordingLogger
) -> None:
    # Redis failed and then recovered, but no request arrived to notice: an idle
    # (staging, night-time) service must not stay "degraded" forever.
    clock = _Clock()
    limiter = _breaker_limiter(clock)
    _break_backend(monkeypatch, limiter)
    await limiter.check(RateLimitTier.DEFAULT, "t", "u")
    assert limiter.failing_open and _degraded() == 1.0

    clock.now += 4.9  # still inside the cooldown: requests are being served unlimited
    assert limiter.failing_open and _degraded() == 1.0

    clock.now += 0.2  # cooldown over: the next request probes Redis, nothing is skipped
    assert not limiter.failing_open
    assert _degraded() == 0.0


def test_a_discarded_limiter_stops_counting_towards_the_gauge() -> None:
    import gc

    limiter = _breaker_limiter(_Clock())
    limiter._degraded = True
    limiter._skip_until = float("inf")
    assert _degraded() == 1.0

    del limiter
    gc.collect()

    assert _degraded() == 0.0


def test_a_disabled_limiter_never_registers_for_the_gauge() -> None:
    limiter = _limiter(rate_limit_enabled=False)
    limiter._degraded = True
    limiter._skip_until = float("inf")

    assert not limiter.enabled
    assert _degraded() == 0.0
