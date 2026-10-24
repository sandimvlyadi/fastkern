"""Optional /metrics protection and baseline security headers."""

import secrets

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.core.config import Environment, Settings
from app.main import create_app

METRICS_TOKEN = "metrics-token-for-tests-only"  # not a real credential


def _settings(environment: Environment = Environment.LOCAL, **overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "environment": environment,
        # Generated at runtime, not a literal: a hardcoded random-looking string here trips
        # gitleaks' generic-api-key rule even though it's not a real secret.
        "secret_key": secrets.token_urlsafe(64),
        "debug": False,
        "email_backend": "smtp",  # production refuses the log-only console backend
        "smtp_username": "mailer",
        "smtp_password": "not-a-real-password",
        "database_url": "sqlite+aiosqlite:///./unit-test.db",
        "enable_metrics": True,
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def _client(settings: Settings) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=create_app(settings)), base_url="http://test")


# --- Optional /metrics protection -----------------------------------------------------------------


async def test_metrics_is_open_when_no_token_is_configured() -> None:
    async with _client(_settings()) as client:
        response = await client.get("/metrics")

    assert response.status_code == 200
    assert "http_requests_total" in response.text or "python_info" in response.text


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-token"},
        {"Authorization": f"Basic {METRICS_TOKEN}"},
        {"Authorization": METRICS_TOKEN},  # no scheme
        {"Authorization": "Bearer "},
        # Raw non-ASCII bytes (httpx won't encode a non-ASCII str): must not blow up compare_digest.
        {"Authorization": b"Bearer \xc3\xbcn\xc3\xafcode"},
    ],
)
async def test_metrics_requires_the_bearer_token_when_configured(
    headers: dict[str, str | bytes],
) -> None:
    settings = _settings(metrics_auth_token=SecretStr(METRICS_TOKEN))

    async with _client(settings) as client:
        response = await client.get("/metrics", headers=headers)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    assert "python_info" not in response.text


async def test_metrics_accepts_the_correct_token_case_insensitively_on_the_scheme() -> None:
    settings = _settings(metrics_auth_token=SecretStr(METRICS_TOKEN))

    async with _client(settings) as client:
        ok = await client.get("/metrics", headers={"Authorization": f"Bearer {METRICS_TOKEN}"})
        lower = await client.get("/metrics", headers={"Authorization": f"bearer {METRICS_TOKEN}"})

    assert ok.status_code == 200
    assert lower.status_code == 200


async def test_an_empty_token_setting_means_unprotected_not_locked_out() -> None:
    async with _client(_settings(metrics_auth_token=SecretStr(""))) as client:
        assert (await client.get("/metrics")).status_code == 200


# --- Security headers -----------------------------------------------------------------------------


async def test_api_responses_carry_the_security_headers_even_on_errors() -> None:
    async with _client(_settings()) as client:
        response = await client.get("/api/v1/auth/me")  # 401: no token

    assert response.status_code == 401
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Cache-Control"] == "no-store"


async def test_probe_and_docs_get_the_headers_but_are_not_marked_no_store() -> None:
    async with _client(_settings()) as client:
        health = await client.get("/healthz")
        docs = await client.get("/docs")

    for response in (health, docs):
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert "Cache-Control" not in response.headers
    assert "Content-Security-Policy" not in docs.headers  # would break the CDN-served Swagger UI


async def test_hsts_is_sent_only_in_production() -> None:
    async with _client(_settings(Environment.LOCAL)) as local:
        assert "Strict-Transport-Security" not in (await local.get("/healthz")).headers
    async with _client(_settings(Environment.STAGING)) as staging:
        assert "Strict-Transport-Security" not in (await staging.get("/healthz")).headers
    async with _client(_settings(Environment.PRODUCTION)) as production:
        hsts = (await production.get("/healthz")).headers["Strict-Transport-Security"]

    assert hsts.startswith("max-age=31536000")
    assert "preload" not in hsts


async def test_existing_response_headers_are_not_overridden() -> None:
    # e.g. the 429 Retry-After / X-Request-ID / CORS headers must survive untouched.
    async with _client(_settings()) as client:
        response = await client.get("/healthz", headers={"X-Request-ID": "keep-me"})

    assert response.headers["X-Request-ID"] == "keep-me"
