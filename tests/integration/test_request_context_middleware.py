import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.core.context import get_request_context
from app.core.middleware import RequestContextMiddleware


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware, settings=get_settings())

    @app.get("/ctx")
    async def ctx() -> dict[str, str]:
        context = get_request_context()
        return {
            "request_id": context.request_id,
            "locale": context.locale,
            "timezone": str(context.timezone),
        }

    return app


async def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=_build_app()), base_url="http://test")


async def test_request_id_header_is_echoed_back() -> None:
    async with await _client() as client:
        response = await client.get("/ctx", headers={"X-Request-ID": "my-request-id"})
    assert response.headers["X-Request-ID"] == "my-request-id"
    assert response.json()["request_id"] == "my-request-id"


async def test_request_id_is_generated_when_absent() -> None:
    async with await _client() as client:
        response = await client.get("/ctx")
    generated = response.headers["X-Request-ID"]
    uuid.UUID(generated)  # must be a valid UUID
    assert response.json()["request_id"] == generated


async def test_accept_language_negotiates_locale() -> None:
    async with await _client() as client:
        response = await client.get("/ctx", headers={"Accept-Language": "id-ID"})
    assert response.json()["locale"] == "id"


async def test_x_timezone_header_is_honored() -> None:
    async with await _client() as client:
        response = await client.get("/ctx", headers={"X-Timezone": "Asia/Jakarta"})
    assert response.json()["timezone"] == "Asia/Jakarta"


async def test_invalid_x_timezone_falls_back_to_default() -> None:
    async with await _client() as client:
        response = await client.get("/ctx", headers={"X-Timezone": "Not/AZone"})
    assert response.json()["timezone"] == "UTC"


async def test_context_is_reset_after_the_request() -> None:
    async with await _client() as client:
        await client.get("/ctx", headers={"X-Request-ID": "should-not-leak"})
    assert get_request_context().request_id == "-"
