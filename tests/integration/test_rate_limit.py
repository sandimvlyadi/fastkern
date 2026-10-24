from collections.abc import Callable, Generator

import pytest
from httpx import AsyncClient, Response
from limits.aio.storage import MemoryStorage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.rate_limit import (
    LIMIT_HEADER,
    REMAINING_HEADER,
    RETRY_AFTER_HEADER,
    RateLimiter,
    build_rate_limiter,
    get_rate_limiter,
)
from app.main import app
from tests.helpers import auth_header, grant_role, register_and_login

MakeLimiter = Callable[..., RateLimiter]


@pytest.fixture
def make_limiter() -> Generator[MakeLimiter]:
    """Installs an in-memory limiter (no Redis, fresh buckets per test) in
    place of the process-wide one, which pytest's env leaves disabled."""

    def _make(**limits: object) -> RateLimiter:
        settings = Settings(  # type: ignore[arg-type]
            secret_key="x" * 32,
            database_url="sqlite+aiosqlite:///./unit-test.db",
            rate_limit_enabled=limits.pop("enabled", True),
            **{f"rate_limit_{tier}": value for tier, value in limits.items()},
        )
        limiter = build_rate_limiter(settings, storage=MemoryStorage())
        app.dependency_overrides[get_rate_limiter] = lambda: limiter
        return limiter

    yield _make
    app.dependency_overrides.pop(get_rate_limiter, None)


async def _login(
    client: AsyncClient, tenant_id: str, headers: dict[str, str] | None = None
) -> Response:
    return await client.post(
        "/api/v1/auth/login",
        json={"tenantId": tenant_id, "email": "nobody@example.com", "password": "S3curePass!"},
        headers=headers,
    )


async def test_login_is_limited_per_ip_and_returns_the_error_envelope(
    client: AsyncClient, tenant_id: str, make_limiter: MakeLimiter
) -> None:
    make_limiter(auth="2/minute")

    first = await _login(client, tenant_id)
    second = await _login(client, tenant_id)
    third = await _login(client, tenant_id)

    assert first.status_code == 401  # wrong credentials still consume an attempt
    assert second.status_code == 401
    assert third.status_code == 429
    body = third.json()
    assert body["error"]["code"] == "rate_limited"
    assert body["error"]["message"] == "Too many requests."
    assert body["error"]["details"]["retry_after"] >= 1
    assert body["meta"]["request_id"]
    assert int(third.headers[RETRY_AFTER_HEADER]) >= 1
    assert third.headers[LIMIT_HEADER] == "2"
    assert third.headers[REMAINING_HEADER] == "0"


async def test_rate_limited_message_is_localized(
    client: AsyncClient, tenant_id: str, make_limiter: MakeLimiter
) -> None:
    make_limiter(auth="1/minute")

    await _login(client, tenant_id)
    limited = await _login(client, tenant_id, headers={"Accept-Language": "id"})

    assert limited.status_code == 429
    assert limited.json()["error"]["message"] == "Terlalu banyak permintaan."


async def test_operational_endpoints_are_never_limited(
    client: AsyncClient, make_limiter: MakeLimiter
) -> None:
    make_limiter(default="1/minute", auth="1/minute", write="1/minute")

    for path in ("/healthz", "/readyz", "/metrics", "/docs", "/redoc", "/openapi.json"):
        responses = [await client.get(path) for _ in range(5)]
        assert 429 not in {r.status_code for r in responses}, path
        assert all(LIMIT_HEADER not in r.headers for r in responses), path


async def test_authenticated_endpoints_are_limited_per_user_and_expose_headers(
    client: AsyncClient, tenant_id: str, make_limiter: MakeLimiter
) -> None:
    make_limiter(default="4/minute", auth="100/minute")
    _, token_a, _ = await register_and_login(client, tenant_id, "rl-a@example.com")  # 1 hit
    _, token_b, _ = await register_and_login(client, tenant_id, "rl-b@example.com")  # 1 hit

    remaining = []
    for _ in range(3):
        response = await client.get("/api/v1/auth/me", headers=auth_header(token_a))
        assert response.status_code == 200
        assert response.headers[LIMIT_HEADER] == "4"
        remaining.append(int(response.headers[REMAINING_HEADER]))
    exhausted = await client.get("/api/v1/auth/me", headers=auth_header(token_a))
    other_user = await client.get("/api/v1/auth/me", headers=auth_header(token_b))

    assert remaining == [2, 1, 0]
    assert exhausted.status_code == 429
    assert other_user.status_code == 200  # same IP + tenant, different user: own bucket


async def test_write_tier_is_stricter_than_default_and_headers_show_the_tighter_one(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    make_limiter: MakeLimiter,
) -> None:
    make_limiter(default="50/minute", auth="100/minute", write="2/minute")
    user_id, token, _ = await register_and_login(client, tenant_id, "rl-writer@example.com")
    await grant_role(
        session_factory,
        tenant_id=tenant_id,
        user_id=user_id,
        role_name="rl-writer",
        permissions=["users:read", "users:write"],
    )

    async def _patch() -> Response:
        return await client.patch(
            f"/api/v1/users/{user_id}", json={"fullName": "X"}, headers=auth_header(token)
        )

    first = await _patch()
    assert first.status_code == 200
    assert first.headers[LIMIT_HEADER] == "2"  # WRITE (2) is tighter than DEFAULT (50)
    assert (await _patch()).status_code == 200
    assert (await _patch()).status_code == 429
    # Reads are governed by DEFAULT only, so they still work.
    read = await client.get(f"/api/v1/users/{user_id}", headers=auth_header(token))
    assert read.status_code == 200


async def test_requests_rejected_by_auth_still_count_against_the_ip_limit(
    client: AsyncClient, make_limiter: MakeLimiter
) -> None:
    make_limiter(ip="3/minute")

    garbage = {"Authorization": "Bearer not-a-real-token"}
    statuses = [(await client.get("/api/v1/users", headers=garbage)).status_code for _ in range(5)]
    no_header = await client.get("/api/v1/users")

    assert statuses == [401, 401, 401, 429, 429]
    assert no_header.status_code == 429  # a missing token counts too, from the same IP
    assert int(no_header.headers[RETRY_AFTER_HEADER]) >= 1


async def test_ip_limit_also_covers_auth_endpoints_and_leaves_operational_ones_alone(
    client: AsyncClient, tenant_id: str, make_limiter: MakeLimiter
) -> None:
    make_limiter(ip="2/minute", auth="100/minute")

    logins = [(await _login(client, tenant_id)).status_code for _ in range(3)]
    health = await client.get("/healthz")

    assert logins == [401, 401, 429]
    assert health.status_code == 200


async def test_forbidden_requests_count_against_the_default_limit_on_tenants_and_audit_logs(
    client: AsyncClient, tenant_id: str, make_limiter: MakeLimiter
) -> None:
    make_limiter(default="5/minute", auth="100/minute")
    _, token, _ = await register_and_login(client, tenant_id, "rl-nobody@example.com")  # 1 hit

    for path in ("/api/v1/tenants", "/api/v1/audit-logs"):
        # No roles granted: authenticated but forbidden. Hits 2..3, then 4..5.
        first = await client.get(path, headers=auth_header(token))
        second = await client.get(path, headers=auth_header(token))
        assert (first.status_code, second.status_code) == (403, 403), path
    exhausted = await client.get("/api/v1/tenants", headers=auth_header(token))

    assert exhausted.status_code == 429  # 403s were counted, so the bucket ran dry


async def test_disabled_limiter_never_returns_429(
    client: AsyncClient, tenant_id: str, make_limiter: MakeLimiter
) -> None:
    make_limiter(enabled=False, auth="1/minute")

    statuses = {(await _login(client, tenant_id)).status_code for _ in range(4)}

    assert statuses == {401}


async def test_redis_outage_fails_open(
    client: AsyncClient,
    tenant_id: str,
    make_limiter: MakeLimiter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limiter = make_limiter(auth="1/minute")

    async def _boom(*_: object, **__: object) -> bool:
        raise ConnectionError("redis is down")

    assert limiter._strategy is not None
    monkeypatch.setattr(limiter._strategy, "hit", _boom)

    statuses = {(await _login(client, tenant_id)).status_code for _ in range(3)}

    assert statuses == {401}  # served normally, never 429/500


async def test_metrics_endpoint_exposes_the_limiter_health_and_flags_a_failing_open_limiter(
    client: AsyncClient,
    tenant_id: str,
    make_limiter: MakeLimiter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limiter = make_limiter(auth="1/minute")

    healthy = (await client.get("/metrics")).text
    assert "rate_limit_backend_degraded 0.0" in healthy
    assert 'rate_limit_backend_errors_total{tier="auth"}' in healthy

    async def _boom(*_: object, **__: object) -> bool:
        raise ConnectionError("redis is down")

    assert limiter._strategy is not None
    monkeypatch.setattr(limiter._strategy, "hit", _boom)
    assert (await _login(client, tenant_id)).status_code == 401  # served, fail-open

    degraded = (await client.get("/metrics")).text
    assert "rate_limit_backend_degraded 1.0" in degraded
