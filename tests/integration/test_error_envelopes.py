"""What the client (and the logs) see on the error paths that used to misbehave:
500s without a request id and 422s that crashed or echoed the request back."""

import uuid

import httpx
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.core.errors import register_exception_handlers
from app.core.middleware import RequestContextMiddleware
from tests.helpers import PASSWORD

REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"


def _app_with_crashing_route() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware, settings=get_settings())
    register_exception_handlers(app)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("kaboom with a secret detail")

    return app


async def _boom(headers: dict[str, str] | None = None) -> httpx.Response:
    # The 500 handler's response is delivered, then Starlette re-raises for the server's log;
    # `raise_app_exceptions=False` makes the test client behave like a real HTTP client.
    transport = ASGITransport(app=_app_with_crashing_route(), raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/boom", headers=headers)


# --- 500 carries the request id -------------------------------------------------------------------


async def test_500_echoes_the_client_request_id_in_body_and_header() -> None:
    response = await _boom({"X-Request-ID": "trace-me-123"})

    assert response.status_code == 500
    assert response.headers["X-Request-ID"] == "trace-me-123"
    body = response.json()
    assert body["meta"]["request_id"] == "trace-me-123"
    assert body["error"]["code"] == "internal_error"
    assert "kaboom" not in response.text  # no leak of the exception detail


async def test_500_generates_a_request_id_when_the_client_sent_none() -> None:
    response = await _boom()

    generated = response.headers["X-Request-ID"]
    uuid.UUID(generated)
    assert response.json()["meta"]["request_id"] == generated


async def test_500_message_honours_accept_language() -> None:
    # The locale is lost with the contextvar too; it must come from request.state.
    response = await _boom({"Accept-Language": "id"})

    assert response.json()["error"]["message"] == "Terjadi kesalahan yang tidak terduga."


# --- non-JSON bodies are a 422, not a 500 ---------------------------------------------------------


async def test_form_urlencoded_body_is_422_not_500(client: AsyncClient) -> None:
    response = await client.post(
        LOGIN, content="a=b", headers={"content-type": "application/x-www-form-urlencoded"}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_text_plain_body_is_422(client: AsyncClient) -> None:
    response = await client.post(LOGIN, content="hello", headers={"content-type": "text/plain"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_binary_body_is_422(client: AsyncClient) -> None:
    response = await client.post(
        LOGIN,
        content=b"\xff\xfe\x00\x01 not json",
        headers={"content-type": "application/octet-stream"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_malformed_json_is_422(client: AsyncClient) -> None:
    response = await client.post(
        LOGIN, content=b'{"email": ', headers={"content-type": "application/json"}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_missing_field_still_reports_loc_and_msg(client: AsyncClient) -> None:
    response = await client.post(LOGIN, json={"email": "a@example.com"})

    assert response.status_code == 422
    errors = response.json()["error"]["details"]["errors"]
    missing = next(e for e in errors if e["loc"] == ["body", "tenantId"])
    assert missing["type"] == "missing"
    assert missing["msg"]


# --- 422 does not reflect what the client sent ----------------------------------------------------


async def test_validation_error_does_not_echo_the_rejected_password(
    client: AsyncClient, tenant_id: str
) -> None:
    payload = {"tenantId": tenant_id, "email": "b2@example.com", "password": "pw7", "fullName": "A"}

    response = await client.post(REGISTER, json=payload)

    assert response.status_code == 422
    assert "pw7" not in response.text
    for error in response.json()["error"]["details"]["errors"]:
        assert set(error) == {"type", "loc", "msg"}  # no `input`, no `ctx`


async def test_validation_error_does_not_echo_a_valid_password_either(
    client: AsyncClient, tenant_id: str
) -> None:
    # Another field fails; the (valid) password the client sent must not come back.
    payload = {
        "tenantId": tenant_id,
        "email": "not-an-email",
        "password": PASSWORD,
        "fullName": "A",
    }

    response = await client.post(REGISTER, json=payload)

    assert response.status_code == 422
    assert PASSWORD not in response.text
