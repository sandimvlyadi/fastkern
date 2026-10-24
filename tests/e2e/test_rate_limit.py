"""Rate limiting against the running stack, with the deliberately LOW limits of
docker-compose.e2e-ratelimit.yml (`make e2e-ratelimit` applies it and restores the defaults after).

Order matters and is deliberate: buckets live in Redis for a minute, so the per-user tiers are
exercised first (with seeded users that log in once each) and the per-IP AUTH tier -- which also
throttles register/login/refresh -- last.
"""

import os

import httpx
import pytest
import redis as redis_lib

from tests.e2e.conftest import BASE_URL, Api, Tokens, error_code, expect, setting

pytestmark = pytest.mark.e2e_ratelimit

AUTH_LIMIT = int(os.environ.get("E2E_RATE_LIMIT_AUTH", "5"))
DEFAULT_LIMIT = int(os.environ.get("E2E_RATE_LIMIT_DEFAULT", "20"))
WRITE_LIMIT = int(os.environ.get("E2E_RATE_LIMIT_WRITE", "5"))
UNKNOWN_USER = "00000000-0000-0000-0000-00000000dead"


@pytest.fixture(scope="module", autouse=True)
def _fresh_rate_limit_windows() -> None:
    """Counters live in Redis for the whole window; clear them so re-running right after a
    previous run doesn't start from an already-exhausted bucket."""
    url = setting("REDIS_URL", "redis://localhost:6379/0")
    try:
        client = redis_lib.Redis.from_url(url)
        for key in client.scan_iter("LIMITS:*"):
            client.delete(key)
    except redis_lib.RedisError:
        pytest.skip(f"cannot reach Redis at {url} to reset the rate-limit windows")


def test_the_default_tier_limits_a_user_and_answers_429_in_the_envelope(
    api: Api, plain_user: Tokens
) -> None:
    codes = [api.get("/auth/me", plain_user.access).status_code for _ in range(DEFAULT_LIMIT + 3)]

    assert codes[:DEFAULT_LIMIT] == [200] * DEFAULT_LIMIT
    assert codes[DEFAULT_LIMIT:] == [429] * 3

    limited = api.get("/auth/me", plain_user.access)
    expect(limited, 429, "rate_limited")
    assert int(limited.headers["Retry-After"]) >= 1
    assert limited.headers["X-RateLimit-Limit"] == str(DEFAULT_LIMIT)
    assert limited.headers["X-RateLimit-Remaining"] == "0"
    assert "X-RateLimit-Reset" in limited.headers


def test_limits_are_per_user_not_shared(api: Api, plain_user: Tokens, superadmin: Tokens) -> None:
    # `plain_user` is exhausted by the previous test; another user is unaffected.
    assert api.get("/auth/me", plain_user.access).status_code == 429
    expect(api.get("/auth/me", superadmin.access), 200)


def test_successful_responses_report_the_remaining_budget(api: Api, superadmin: Tokens) -> None:
    first = expect(api.get("/auth/me", superadmin.access), 200)
    second = expect(api.get("/auth/me", superadmin.access), 200)

    assert first.headers["X-RateLimit-Limit"] == str(DEFAULT_LIMIT)
    assert (
        int(second.headers["X-RateLimit-Remaining"])
        == int(first.headers["X-RateLimit-Remaining"]) - 1
    )


def test_the_write_tier_is_tighter_than_the_default_tier_for_mutations(
    api: Api, admin: Tokens
) -> None:
    # Unknown user id: a 404 after authn/authz, so each call spends WRITE budget, side-effect free.
    codes = [
        api.patch(f"/users/{UNKNOWN_USER}", admin.access, json={}).status_code
        for _ in range(WRITE_LIMIT + 3)
    ]

    assert codes[:WRITE_LIMIT] == [404] * WRITE_LIMIT
    assert codes[WRITE_LIMIT:] == [429] * 3
    # Reads are governed by the (larger) default bucket and still work for the same user.
    expect(api.get("/auth/me", admin.access), 200)


def test_the_auth_tier_throttles_login_by_ip_and_probes_are_never_limited(api: Api) -> None:
    """Last on purpose: exhausting the per-IP AUTH bucket also blocks register/login/refresh."""
    codes = []
    for _ in range(AUTH_LIMIT + 4):
        codes.append(api.login("nobody@example.com", "x").status_code)
        if codes[-1] == 429 and codes.count(429) >= 3:
            break

    first_429 = codes.index(429)
    assert 1 <= first_429 <= AUTH_LIMIT  # earlier logins by the fixtures spent part of the bucket
    assert set(codes[:first_429]) == {401}
    assert set(codes[first_429:]) == {429}

    limited = api.login("nobody@example.com", "x")
    assert error_code(limited) == "rate_limited"
    assert "Retry-After" in limited.headers
    # other auth-tier endpoints share the exhausted bucket
    assert api.post("/auth/refresh", json={"refreshToken": "x"}).status_code == 429

    # Probes and docs are never limited, even now.
    for path in ("/healthz", "/readyz", "/metrics"):
        for _ in range(60):
            assert httpx.get(f"{BASE_URL}{path}", timeout=20).status_code == 200
