"""`ApiTrafficLogMiddleware` in isolation: the request-log dispatch never holds up the response,
and an unhandled error is still logged."""

from typing import Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.core.middleware import api_traffic_log
from app.core.middleware.api_traffic_log import ApiTrafficLogMiddleware

API_KEY_ID = uuid4()
TENANT_ID = uuid4()


def _authenticate(request: Request) -> None:
    request.state.api_client_id = API_KEY_ID
    request.state.api_client_tenant_id = TENANT_ID


async def partner_ok(request: Request) -> JSONResponse:
    _authenticate(request)
    return JSONResponse({"ok": True})


async def partner_boom(request: Request) -> JSONResponse:
    _authenticate(request)
    raise RuntimeError("boom")


async def anonymous(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


def _app() -> Starlette:
    app = Starlette(
        routes=[
            Route("/partner", partner_ok),
            Route("/partner-boom", partner_boom),
            Route("/anonymous", anonymous),
        ]
    )
    app.add_middleware(ApiTrafficLogMiddleware)
    return app


@pytest.fixture
def dispatched(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def _capture(name: str, **kwargs: Any) -> None:
        assert name == "api_clients.record_request_log"
        calls.append(kwargs)

    monkeypatch.setattr(api_traffic_log, "dispatch_task", _capture)
    return calls


async def test_partner_request_is_logged_after_the_response(
    dispatched: list[dict[str, Any]],
) -> None:
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t") as client:
        response = await client.get("/partner")

    assert response.status_code == 200
    [payload] = dispatched
    assert payload["api_key_id"] == str(API_KEY_ID)
    assert payload["tenant_id"] == str(TENANT_ID)
    assert payload["status_code"] == 200
    assert payload["path"] == "/partner"
    assert payload["used_at"]


async def test_a_broker_failure_costs_only_the_log_row(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _broker_down(name: str, **kwargs: Any) -> None:
        raise ConnectionError("broker unreachable")

    monkeypatch.setattr(api_traffic_log, "dispatch_task", _broker_down)
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t") as client:
        response = await client.get("/partner")

    assert response.status_code == 200


async def test_an_unhandled_error_is_logged_as_a_500(dispatched: list[dict[str, Any]]) -> None:
    transport = ASGITransport(app=_app(), raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        response = await client.get("/partner-boom")

    assert response.status_code == 500
    [payload] = dispatched
    assert payload["status_code"] == 500
    assert payload["response_size_bytes"] is None


async def test_a_request_without_an_api_key_is_not_logged(
    dispatched: list[dict[str, Any]],
) -> None:
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t") as client:
        await client.get("/anonymous")

    assert dispatched == []
