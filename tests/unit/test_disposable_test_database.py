import pytest
from sqlalchemy.engine import make_url

from tests.helpers import is_disposable_test_database


@pytest.mark.parametrize(
    "url",
    [
        "sqlite+aiosqlite:///./test.db",
        "sqlite+aiosqlite:///./anything-at-all.db",
        "postgresql+asyncpg://u:p@localhost:5432/fastkern_test",
        "postgresql+asyncpg://u:p@localhost:5432/app_test",
        "postgresql+asyncpg://u:p@localhost:5432/test_app",
        "postgresql+asyncpg://u:p@localhost:5432/test",
        "postgresql+asyncpg://u:p@localhost:5432/tests",
        "postgresql+asyncpg://u:p@localhost:5432/app-test",
        "postgresql+asyncpg://u:p@localhost:5432/APP_TEST",
    ],
)
def test_databases_the_schema_fixture_may_drop(url: str) -> None:
    assert is_disposable_test_database(make_url(url)) is True


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://u:p@localhost:5432/fastkern",
        "postgresql+asyncpg://u:p@localhost:5432/appdata",
        # Substring matches: these contain "test" but are not test databases.
        "postgresql+asyncpg://u:p@localhost:5432/latest",
        "postgresql+asyncpg://u:p@localhost:5432/contest",
        "postgresql+asyncpg://u:p@localhost:5432/attestation_service",
        "postgresql+asyncpg://u:p@localhost:5432/testing",
        "postgresql+asyncpg://u:p@localhost:5432/pgtests",
        "postgresql+asyncpg://u:p@localhost:5432/",
    ],
)
def test_databases_the_schema_fixture_must_refuse(url: str) -> None:
    assert is_disposable_test_database(make_url(url)) is False
