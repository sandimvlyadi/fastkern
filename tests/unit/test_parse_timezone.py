"""An untrusted timezone value must never fail a request (found by the e2e suite: an
`X-Timezone: ../../etc/passwd` header used to 500 every endpoint)."""

from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.core.context import get_request_context
from app.core.middleware import RequestContextMiddleware
from app.core.timezone import parse_timezone


@pytest.mark.parametrize("key", ["UTC", "Asia/Jakarta", "America/New_York"])
def test_valid_keys_parse(key: str) -> None:
    assert parse_timezone(key) == ZoneInfo(key)


@pytest.mark.parametrize(
    "key",
    [
        "Mars/Olympus",  # unknown -> ZoneInfoNotFoundError
        "../../etc/passwd",  # path-shaped -> ValueError
        "/etc/passwd",
        "Asia/../../x",
        "with\x00nul",  # embedded NUL -> ValueError
        "",
        "a" * 5000,  # over-long
        "America",  # a directory, not a zone
        "..",
    ],
)
def test_unusable_keys_return_none_instead_of_raising(key: str) -> None:
    assert parse_timezone(key) is None


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware, settings=get_settings())

    @app.get("/tz")
    async def tz() -> dict[str, str]:
        return {"timezone": str(get_request_context().timezone)}

    return app


@pytest.mark.parametrize(
    "header",
    ["../../etc/passwd", "Mars/Olympus", "with\tspace", "a" * 4000, "%00", "America"],
)
async def test_hostile_x_timezone_header_falls_back_to_the_default(header: str) -> None:
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.get("/tz", headers={"X-Timezone": header})

    assert response.status_code == 200
    assert response.json()["timezone"] == get_settings().default_timezone


async def test_a_valid_x_timezone_header_is_still_honoured() -> None:
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.get("/tz", headers={"X-Timezone": "Asia/Jakarta"})

    assert response.json()["timezone"] == "Asia/Jakarta"
