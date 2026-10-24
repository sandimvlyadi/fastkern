"""Structural guard: every API route is rate limited, in the right order, and
the operational endpoints are not.

Same idea as `test_i18n_key_coverage.py` — a new endpoint that forgets
`Depends(rate_limit(...))`, or puts it after auth/permission checks, fails
`make test` instead of shipping with a hole.
"""

from typing import Any

from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, iter_route_contexts
from starlette.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.core.di import get_current_api_client, get_current_user
from app.core.rate_limit import RATE_LIMIT_RESPONSE_HEADERS, RateLimitDependency, RateLimitTier
from app.main import app

# Never rate limited: orchestrator probes / Prometheus scrapes / FastAPI docs.
_UNLIMITED_PATHS = {"/healthz", "/readyz", "/metrics", "/docs", "/redoc", "/openapi.json"}


def _execution_order(dependant: Dependant) -> list[Any]:
    """Dependency callables in the order FastAPI runs them (a dependency's own
    sub-dependencies run before it). A callable reused in several places
    appears several times; the first occurrence is when it actually runs."""
    calls: list[Any] = []
    for sub in dependant.dependencies:
        calls += _execution_order(sub)
        calls.append(sub.call)
    return calls


def _label(call: Any) -> str | None:
    if isinstance(call, RateLimitDependency):
        return f"rl:{call.tier.value}"
    if call is get_current_user:
        return "auth"
    # Partner (API-key) auth is a distinct principal/tier from human `auth`, but plays the
    # same structural role in the ordering checks below.
    if call is get_current_api_client:
        return "api_auth"
    qualname = getattr(call, "__qualname__", "")
    if qualname.startswith(("require_permission.", "require_role.", "require_scope.")):
        return "perm"
    return None


def _sequence(route: Any) -> list[str]:
    """Labelled dependencies of a route, in execution order, first occurrence only."""
    seen: list[str] = []
    for label in map(_label, _execution_order(route.dependant)):
        if label is not None and label not in seen:
            seen.append(label)
    return seen


def _all_routes() -> list[Any]:
    # `include_router` wraps routers lazily (`app.routes` doesn't list their
    # endpoints); `iter_route_contexts` yields every effective route with the
    # router-level dependencies already merged into `dependant`.
    return [rc for rc in iter_route_contexts(app.routes) if isinstance(rc.original_route, APIRoute)]


def _api_routes() -> list[Any]:
    prefix = get_settings().api_v1_prefix
    return [r for r in _all_routes() if r.path.startswith(prefix)]


def _name(route: Any) -> str:
    return f"{sorted(route.methods)} {route.path}"


def test_there_are_api_routes_to_check() -> None:
    # Guards every check below against silently iterating over nothing.
    assert len(_api_routes()) >= 15


def test_every_api_route_has_the_ip_tier_and_a_per_caller_tier() -> None:
    missing = [
        _name(r)
        for r in _api_routes()
        if "rl:ip" not in _sequence(r)
        or not {"rl:default", "rl:auth", "rl:partner"} & set(_sequence(r))
    ]
    assert missing == [], f"API routes missing rate_limit(IP) or DEFAULT/AUTH/PARTNER: {missing}"


def test_ip_tier_runs_before_anything_that_can_reject_the_request() -> None:
    late = [_name(r) for r in _api_routes() if _sequence(r)[0] != "rl:ip"]
    assert late == [], (
        f"rate_limit(IP) must be the first dependency, or requests rejected by "
        f"auth would never be counted: {late}"
    )


def test_user_keyed_tiers_run_after_authentication() -> None:
    early = []
    for route in _api_routes():
        seq = _sequence(route)
        # Pre-auth routes (e.g. logout) are IP-keyed by design and have neither label.
        auth_label = "auth" if "auth" in seq else ("api_auth" if "api_auth" in seq else None)
        if auth_label is None:
            continue
        tiers = ("rl:default", "rl:write") if auth_label == "auth" else ("rl:partner",)
        for tier in tiers:
            if tier in seq and seq.index(auth_label) > seq.index(tier):
                early.append(f"{_name(route)} ({tier})")
    assert early == [], f"tiers keyed on tenant/user/api_client ran before auth: {early}"


def test_default_tier_runs_before_permission_checks() -> None:
    uncounted = []
    for r in _api_routes():
        seq = _sequence(r)
        if "perm" not in seq:
            continue
        # DEFAULT for human-authenticated routes, PARTNER for API-key-authenticated ones.
        per_caller_tier = "rl:default" if "rl:default" in seq else "rl:partner"
        if per_caller_tier in seq and seq.index(per_caller_tier) > seq.index("perm"):
            uncounted.append(_name(r))
    assert uncounted == [], f"403s on these routes would not count against the quota: {uncounted}"


def test_every_mutating_authenticated_route_is_also_write_limited_or_auth_limited() -> None:
    # `rl:upload` is the chunk-PUT tier of the files module:
    # WRITE's 30/minute would throttle a multi-GB upload to minutes.
    weaker = [
        _name(r)
        for r in _api_routes()
        if r.methods & {"POST", "PATCH", "PUT", "DELETE"}
        and r.path != f"{get_settings().api_v1_prefix}/auth/logout"  # pre-auth, IP-keyed
        and not {"rl:write", "rl:auth", "rl:upload"} & set(_sequence(r))
    ]
    assert weaker == [], f"mutating routes with only the default tier: {weaker}"


def test_every_rate_limited_route_documents_429() -> None:
    undocumented = [_name(r) for r in _api_routes() if 429 not in r.responses]
    assert undocumented == [], f"routes missing RateLimitedError in error_responses: {undocumented}"


def test_operational_endpoints_are_not_rate_limited() -> None:
    checked = set()
    for route in _all_routes():
        if route.path in _UNLIMITED_PATHS:
            checked.add(route.path)
            assert not any(label.startswith("rl:") for label in _sequence(route)), route.path
    assert {"/healthz", "/readyz", "/metrics"} <= checked


def test_cors_exposes_the_rate_limit_headers_to_browser_clients() -> None:
    # Not CORS-safelisted: without expose_headers a browser on another origin
    # can't read Retry-After after a 429.
    cors = next(m for m in app.user_middleware if m.cls is CORSMiddleware)
    exposed = set(cors.kwargs["expose_headers"])

    assert set(RATE_LIMIT_RESPONSE_HEADERS) <= exposed
    assert "X-Request-ID" in exposed


def test_tier_enum_matches_settings_fields() -> None:
    settings = get_settings()
    for tier in RateLimitTier:
        assert hasattr(settings, f"rate_limit_{tier.value}"), tier
