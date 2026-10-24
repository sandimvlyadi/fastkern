import secrets

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Environment, Settings
from app.main import create_app

_DOC_PATHS = ("/docs", "/redoc", "/openapi.json")


def _settings(environment: Environment, **overrides: object) -> Settings:
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
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


async def _status_codes(settings: Settings) -> dict[str, int]:
    transport = ASGITransport(app=create_app(settings))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return {path: (await client.get(path)).status_code for path in _DOC_PATHS}


async def test_docs_and_schema_are_served_outside_production() -> None:
    assert await _status_codes(_settings(Environment.LOCAL)) == dict.fromkeys(_DOC_PATHS, 200)


async def test_docs_and_schema_are_all_hidden_in_production() -> None:
    # /openapi.json used to stay open while /docs and /redoc were 404 — the schema alone is the
    # whole API surface.
    assert await _status_codes(_settings(Environment.PRODUCTION)) == dict.fromkeys(_DOC_PATHS, 404)


@pytest.mark.parametrize(
    ("environment", "docs_enabled", "expected"),
    [
        (Environment.PRODUCTION, True, 200),  # internal deployment opts in
        (Environment.STAGING, None, 200),
        (Environment.LOCAL, False, 404),  # explicit off wins outside production too
    ],
)
async def test_docs_enabled_setting_overrides_the_environment_default(
    environment: Environment, docs_enabled: bool | None, expected: int
) -> None:
    codes = await _status_codes(_settings(environment, docs_enabled=docs_enabled))
    assert codes == dict.fromkeys(_DOC_PATHS, expected)


async def test_openapi_documents_common_headers(client: AsyncClient) -> None:
    """Regression guard for `register_common_header_docs()` actually being
    wired into `create_app()` (src/app/main.py) — the unit tests in
    `tests/unit/test_core_openapi.py` only exercise that function against
    an isolated bare `FastAPI()` instance, not the real composed app this
    fixture serves requests through."""
    response = await client.get("/openapi.json")
    assert response.status_code == 200

    schema = response.json()
    operation = schema["paths"]["/api/v1/users"]["get"]
    header_names = {p["name"] for p in operation["parameters"] if p["in"] == "header"}
    assert {"Accept-Language", "X-Timezone"} <= header_names
