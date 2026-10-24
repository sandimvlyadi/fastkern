"""Staging/production must refuse to start on a forgeable or leaky configuration."""

import secrets

import pytest
from pydantic import ValidationError

from app.core.config import Environment, Settings

# Generated at runtime, not a literal: a hardcoded random-looking string here trips gitleaks'
# generic-api-key rule even though it's not a real secret. >= 64 chars, none of the placeholder
# words `Settings` checks for.
STRONG_KEY = secrets.token_urlsafe(64)
PUBLIC_PLACEHOLDER = "change-me-to-a-random-64-byte-value"


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "secret_key": STRONG_KEY,
        "debug": False,  # pinned: a developer's .env (DEBUG=true) must not leak into these tests
        # A valid production email setup, so these tests isolate the guard they are about.
        "email_backend": "smtp",
        "smtp_username": "mailer",
        "smtp_password": "not-a-real-password",
        "database_url": "sqlite+aiosqlite:///./unit-test.db",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


@pytest.mark.parametrize("environment", [Environment.LOCAL, Environment.TEST])
@pytest.mark.parametrize("key", [PUBLIC_PLACEHOLDER, "x" * 32])
def test_weak_key_and_debug_allowed_outside_deployed_environments(
    environment: Environment, key: str
) -> None:
    settings = _settings(environment=environment, secret_key=key, debug=True, cors_origins=["*"])
    assert settings.secret_key == key


@pytest.mark.parametrize("environment", [Environment.STAGING, Environment.PRODUCTION])
@pytest.mark.parametrize(
    "key",
    [
        PUBLIC_PLACEHOLDER,
        "changeme" + "Qw3rTy9uIoPaSdFgHjKlZxCvBnM1",
        "please-use-example-" + "Qw3rTy9uIoPaSdFgHjKlZxCv",
        "my-secret-" + "Qw3rTy9uIoPaSdFgHjKlZxCvBnM1234567",
        "a" * 64,
        "ab" * 32,
    ],
)
def test_placeholder_or_low_entropy_key_rejected_when_deployed(
    environment: Environment, key: str
) -> None:
    with pytest.raises(ValidationError) as exc:
        _settings(environment=environment, secret_key=key)
    # The message tells the operator how to fix it.
    assert "secrets.token_urlsafe(64)" in str(exc.value)


def test_production_requires_64_char_key() -> None:
    with pytest.raises(ValidationError, match="at least 64"):
        _settings(environment=Environment.PRODUCTION, secret_key=STRONG_KEY[:40])


def test_staging_accepts_32_char_key() -> None:
    settings = _settings(environment=Environment.STAGING, secret_key=STRONG_KEY[:40])
    assert settings.environment is Environment.STAGING


def test_production_rejects_debug() -> None:
    with pytest.raises(ValidationError, match="DEBUG must be false"):
        _settings(environment=Environment.PRODUCTION, debug=True)


def test_production_rejects_wildcard_cors() -> None:
    with pytest.raises(ValidationError, match="CORS_ORIGINS"):
        _settings(environment=Environment.PRODUCTION, cors_origins=["https://a.example", "*"])


def test_staging_allows_debug_and_wildcard_cors() -> None:
    settings = _settings(environment=Environment.STAGING, debug=True, cors_origins=["*"])
    assert settings.debug is True


def test_valid_production_settings_pass() -> None:
    settings = _settings(
        environment=Environment.PRODUCTION,
        cors_origins=["https://app.acme-corp.io"],
    )
    assert settings.is_production
