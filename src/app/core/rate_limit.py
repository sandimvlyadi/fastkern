"""Async, Redis-backed rate limiting (`limits.aio`), applied per router as a
FastAPI dependency.

Usage: every router lists, in this order,

    router = APIRouter(
        prefix="/orders",
        dependencies=[
            Depends(rate_limit(RateLimitTier.IP)),       # before auth: caps garbage tokens
            Depends(get_current_user),
            Depends(rate_limit(RateLimitTier.DEFAULT)),  # after auth: keys on tenant + user
            Depends(require_permission("orders:read")),  # after the limiter: 403s count too
        ],
    )

`IP` must come before anything that can reject the request (auth,
permission checks), the other tiers AFTER `get_current_user` so the caller's
tenant/user are already in the request context when the bucket key is built.
`tests/unit/test_rate_limit_coverage.py` enforces this ordering.

Tiers (limits come from `Settings`, so they are tunable per environment):

- `IP`: every API endpoint, keyed by client IP alone and checked before
  authentication, so requests rejected by auth (invalid/expired tokens,
  deactivated users) are still counted (`RATE_LIMIT_IP`, deliberately loose:
  an office behind one NAT shares it).
- `DEFAULT`: every API endpoint (`RATE_LIMIT_DEFAULT`).
- `AUTH`: pre-login endpoints — register/login/refresh (`RATE_LIMIT_AUTH`).
- `WRITE`: mutating endpoints, on top of DEFAULT (`RATE_LIMIT_WRITE`).
- `PARTNER`: every `partners` router endpoint (API-key-authenticated, keyed by
  `api_client_id` instead of `user_id`, since a partner's own office may NAT
  many staff through one IP), after `get_current_api_client`
  (`RATE_LIMIT_PARTNER`).

`/healthz`, `/readyz`, `/metrics`, `/docs`, `/redoc` and `/openapi.json` are
not on any API router, so they are never limited (orchestrator probes and
Prometheus scrapes must not get a 429).

If Redis is unreachable the request is let through (fail-open) — a cache
outage must not take the whole API down with it. A small circuit breaker
keeps that cheap: after a failure Redis is skipped entirely for a few seconds
(one warning is logged, not one per request), then a single probe request
checks whether it has recovered. Without it, a black-holed Redis would add
one full timeout per tier to every request.

Failing open is invisible to clients, so it is exported as Prometheus metrics
(served by `/metrics` when `ENABLE_METRICS` is on) for alerting:

- `rate_limit_backend_degraded`: 1 while requests are being served without a
  limit because Redis is being skipped (computed at scrape time, so it drops
  back to 0 on its own once the cooldown ends, even on an idle service).
- `rate_limit_backend_errors_total{tier}`: failed Redis calls.
"""

import math
import time
import weakref
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from typing import Final

from fastapi import Depends, Request, Response
from limits import RateLimitItem
from limits import parse as parse_rate_limit
from limits.aio.storage import RedisStorage, Storage
from limits.aio.strategies import FixedWindowRateLimiter
from prometheus_client import Counter, Gauge

from app.core.config import Settings, get_settings
from app.core.context import get_request_context
from app.core.exceptions import RateLimitedError
from app.core.logging import get_logger

logger = get_logger(__name__)

# A hung Redis must degrade to "fail-open" quickly, not stall every request.
_BACKEND_TIMEOUT_SECONDS: Final = 0.25
# After a backend failure, skip Redis entirely for this long...
_BREAKER_COOLDOWN_SECONDS: Final = 5.0
# ...then let ONE request probe it; concurrent requests keep skipping for this
# long (must exceed the timeout above so the probe can finish first).
_PROBE_LEASE_SECONDS: Final = 1.0

LIMIT_HEADER = "X-RateLimit-Limit"
REMAINING_HEADER = "X-RateLimit-Remaining"
RESET_HEADER = "X-RateLimit-Reset"
RETRY_AFTER_HEADER = "Retry-After"
# Custom response headers a browser client on another origin needs to read;
# `main.py` lists them in CORS `expose_headers` (they aren't CORS-safelisted).
RATE_LIMIT_RESPONSE_HEADERS = (LIMIT_HEADER, REMAINING_HEADER, RESET_HEADER, RETRY_AFTER_HEADER)


class RateLimitTier(StrEnum):
    IP = "ip"
    DEFAULT = "default"
    AUTH = "auth"
    WRITE = "write"
    # Chunk PUTs of the files module; limit comes from RATE_LIMIT_UPLOAD.
    UPLOAD = "upload"
    # Partner (API-key-authenticated) routes; keyed on api_client_id, not user, so one
    # office NAT'ing many staff through one IP shares a single partner-level limit.
    PARTNER = "partner"


# Every live, enabled limiter (there is one per process in production). Weak,
# so a discarded limiter (a test's) stops counting without any cleanup.
_LIMITERS: weakref.WeakSet[RateLimiter] = weakref.WeakSet()


def _failing_open_now() -> float:
    return 1.0 if any(limiter.failing_open for limiter in _LIMITERS) else 0.0


# "Degraded" rather than "up": an unlabelled Gauge is exported as 0 from
# import time, so 0 has to mean healthy or every start (and every deployment
# with RATE_LIMIT_ENABLED=false) would look like an outage. The value is
# computed at scrape time from the breaker's state instead of being `set()`
# on transitions: a `set(1)` would stay 1 after Redis recovered if no request
# ever arrived to notice, and an idle service would page for nothing.
BACKEND_DEGRADED = Gauge(
    "rate_limit_backend_degraded",
    "1 while requests are served without a rate limit because Redis is being skipped, else 0.",
)
BACKEND_DEGRADED.set_function(_failing_open_now)
BACKEND_ERRORS = Counter(
    "rate_limit_backend_errors_total",
    "Redis calls that failed while checking a rate limit.",
    ["tier"],
)


def _export_zero_series() -> None:
    for tier in RateLimitTier:
        BACKEND_ERRORS.labels(tier.value)  # every series exists at 0, so increase() works


_export_zero_series()


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    reset_at: int  # epoch seconds at which the current window resets

    @property
    def retry_after(self) -> int:
        return max(1, self.reset_at - math.ceil(time.time()))

    def headers(self) -> dict[str, str]:
        headers = {
            LIMIT_HEADER: str(self.limit),
            REMAINING_HEADER: str(self.remaining),
            RESET_HEADER: str(self.reset_at),
        }
        if not self.allowed:
            headers[RETRY_AFTER_HEADER] = str(self.retry_after)
        return headers


class RateLimiter:
    """Fixed-window limiter over a `limits.aio` storage, one limit per tier.

    `storage=None` is only valid when `enabled` is False (nothing to talk to).
    """

    def __init__(
        self,
        *,
        limits: dict[RateLimitTier, RateLimitItem],
        storage: Storage | None,
        enabled: bool = True,
        breaker_cooldown_seconds: float = _BREAKER_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limits = limits
        self._strategy = FixedWindowRateLimiter(storage) if storage is not None else None
        self.enabled = enabled and self._strategy is not None
        self._cooldown = breaker_cooldown_seconds
        self._clock = clock
        self._degraded = False  # True from the first backend failure until a probe succeeds
        self._skip_until = 0.0  # monotonic time before which Redis is not even tried
        if self.enabled:
            _LIMITERS.add(self)

    @property
    def failing_open(self) -> bool:
        """True while requests are being served unlimited because Redis is
        being skipped. Not the same as "not yet confirmed recovered": once the
        cooldown ends the next request probes Redis, so nothing is skipped."""
        return self._degraded and self._clock() < self._skip_until

    async def check(
        self, tier: RateLimitTier, *identifiers: str, report_allowed: bool = True
    ) -> RateLimitDecision | None:
        """Consumes one hit from `tier`'s bucket for `identifiers`.

        Returns None when limiting is disabled, the backend is unavailable
        (fail-open), or — with `report_allowed=False` — the hit was allowed:
        callers treat None as "allowed, nothing to report". `report_allowed=
        False` saves the second Redis round trip (the window stats) for a tier
        that never sends headers; a denial always carries its stats.
        """
        if not self.enabled or self._strategy is None or self._skip_backend():
            return None
        item = self._limits[tier]
        try:
            allowed = await self._strategy.hit(item, tier.value, *identifiers)
            if allowed and not report_allowed:
                self._record_success()
                return None
            stats = await self._strategy.get_window_stats(item, tier.value, *identifiers)
        except Exception as exc:
            self._record_failure(tier, exc)
            return None
        self._record_success()

        reset_at = math.ceil(stats.reset_time)
        if not allowed and stats.remaining > 0:
            # hit() and get_window_stats() are separate round trips: the window
            # rolled over in between, so a retry will most likely succeed.
            reset_at = math.ceil(time.time()) + 1
        return RateLimitDecision(
            allowed=allowed,
            limit=item.amount,
            remaining=0 if not allowed else max(0, stats.remaining),
            reset_at=reset_at,
        )

    def _skip_backend(self) -> bool:
        now = self._clock()
        if now < self._skip_until:
            return True
        if self._degraded:
            # Cooldown over: this request probes Redis, everyone else keeps
            # skipping until the probe has had time to finish.
            self._skip_until = now + _PROBE_LEASE_SECONDS
        return False

    def _record_success(self) -> None:
        if self._degraded:
            self._degraded = False
            self._skip_until = 0.0
            logger.info("rate_limit_backend_recovered")

    def _record_failure(self, tier: RateLimitTier, exc: Exception) -> None:
        first_failure = not self._degraded
        self._degraded = True
        self._skip_until = self._clock() + self._cooldown
        BACKEND_ERRORS.labels(tier.value).inc()
        if first_failure:  # one warning per outage, not one per request
            logger.warning(
                "rate_limit_backend_unavailable",
                tier=tier.value,
                error=str(exc),
                skipping_for_seconds=self._cooldown,
            )


def build_rate_limiter(settings: Settings, *, storage: Storage | None = None) -> RateLimiter:
    limits = {
        tier: parse_rate_limit(getattr(settings, f"rate_limit_{tier.value}"))
        for tier in RateLimitTier
    }
    if storage is None and settings.rate_limit_enabled:
        storage = RedisStorage(
            f"async+{settings.redis_url}",
            # `coredis` (limits' default client) isn't a dependency; `redis`
            # already is, and it's what the rest of the app uses.
            implementation="redispy",
            socket_timeout=_BACKEND_TIMEOUT_SECONDS,
            socket_connect_timeout=_BACKEND_TIMEOUT_SECONDS,
        )
    return RateLimiter(limits=limits, storage=storage, enabled=settings.rate_limit_enabled)


@lru_cache
def get_rate_limiter() -> RateLimiter:
    """Process-wide limiter. Override this in tests
    (`app.dependency_overrides[get_rate_limiter]`) to inject an in-memory
    storage instead of Redis."""
    return build_rate_limiter(get_settings())


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _apply_headers(response: Response, headers: dict[str, str]) -> None:
    # An endpoint can sit behind several tiers (DEFAULT + WRITE): report the
    # most restrictive one, i.e. the one with the least remaining quota.
    existing = response.headers.get(REMAINING_HEADER)
    if existing is not None and int(existing) <= int(headers[REMAINING_HEADER]):
        return
    for name, value in headers.items():
        response.headers[name] = value


class RateLimitDependency:
    """FastAPI dependency instance for one tier — build it with `rate_limit()`.

    A class (not a closure) so `tier` is inspectable: `tests/unit/
    test_rate_limit_coverage.py` walks the app's routes and fails if an API
    route has no `RateLimitDependency` in its dependency tree.
    """

    def __init__(self, tier: RateLimitTier) -> None:
        self.tier = tier

    async def __call__(
        self,
        request: Request,
        response: Response,
        limiter: RateLimiter = Depends(get_rate_limiter),
    ) -> None:
        identifiers: tuple[str, ...]
        if self.tier is RateLimitTier.IP:
            # Runs before auth, so it never looks at the (still empty) context.
            identifiers = (_client_ip(request),)
        else:
            ctx = get_request_context()
            tenant = str(ctx.tenant_id) if ctx.tenant_id else "no-tenant"
            # Authenticated callers are keyed by whichever principal authenticated them
            # (user, or — for PARTNER — the ApiKey) so one office NAT'ing many
            # users, or one tenant holding several partner keys, doesn't share a bucket;
            # pre-auth callers can only be keyed by IP.
            if ctx.api_client_id:
                subject = str(ctx.api_client_id)
            elif ctx.user_id:
                subject = str(ctx.user_id)
            else:
                subject = _client_ip(request)
            identifiers = (tenant, subject)

        # The IP tier sends no headers, so a request it allows needs no stats.
        decision = await limiter.check(
            self.tier, *identifiers, report_allowed=self.tier is not RateLimitTier.IP
        )
        if decision is None:
            return
        if not decision.allowed:
            raise RateLimitedError(retry_after=decision.retry_after, headers=decision.headers())
        _apply_headers(response, decision.headers())


def rate_limit(tier: RateLimitTier = RateLimitTier.DEFAULT) -> RateLimitDependency:
    return RateLimitDependency(tier)
