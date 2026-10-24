import secrets

import pytest
from pydantic import ValidationError

from app.core.config import Environment, Settings, get_settings


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "secret_key": "x" * 32,
        "database_url": "sqlite+aiosqlite:///./unit-test.db",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def test_sqlite_database_url_is_allowed() -> None:
    settings = _settings(database_url="sqlite+aiosqlite:///./x.db")
    assert settings.database_url.startswith("sqlite")


def test_postgres_database_url_is_allowed() -> None:
    settings = _settings(database_url="postgresql://user:pass@localhost/db")
    assert settings.database_url == "postgresql://user:pass@localhost/db"


def test_non_postgres_non_sqlite_database_url_rejected() -> None:
    with pytest.raises(ValidationError):
        _settings(database_url="mysql://user:pass@localhost/db")


def test_bad_redis_url_rejected() -> None:
    with pytest.raises(ValidationError):
        _settings(redis_url="http://not-a-redis-url")


def test_is_production_property() -> None:
    settings = _settings(
        environment=Environment.PRODUCTION,
        # Generated at runtime, not a literal: a hardcoded random-looking string here trips
        # gitleaks' generic-api-key rule even though it's not a real secret.
        secret_key=secrets.token_urlsafe(64),
        debug=False,
        email_backend="smtp",
        smtp_username="mailer",
        smtp_password="not-a-real-password",
    )
    assert settings.is_production is True
    assert settings.is_test is False


def test_is_test_property() -> None:
    settings = _settings(environment=Environment.TEST)
    assert settings.is_test is True
    assert settings.is_production is False


def test_get_settings_returns_cached_singleton() -> None:
    assert get_settings() is get_settings()
