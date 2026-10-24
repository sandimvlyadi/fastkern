"""A dead/unreachable database or Redis is a 503 + Retry-After, not a 500;
a bad statement stays a 500 because that is a bug."""

import socket
from collections.abc import AsyncGenerator, Callable

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from kombu import exceptions as kombu_exc
from redis import exceptions as redis_exc
from sqlalchemy import exc as sa_exc
from sqlalchemy.ext.asyncio import AsyncSession

import app.core.celery_app as celery_app_module
from app.core.config import get_settings
from app.core.db import get_db_session
from app.core.errors import register_exception_handlers
from app.core.middleware import RequestContextMiddleware
from app.main import app as real_app
from tests.helpers import auth_header, register_and_login

_STMT = "SELECT 1"
_DRIVER = Exception("driver detail that must never reach the client")

UNREACHABLE: list[Callable[[], BaseException]] = [
    lambda: ConnectionRefusedError("db down"),
    lambda: ConnectionResetError("reset"),
    lambda: socket.gaierror(-2, "Name or service not known"),
    lambda: TimeoutError("timed out"),
    lambda: sa_exc.TimeoutError("QueuePool limit of size 10 overflow 20 reached"),
    lambda: sa_exc.OperationalError(_STMT, {}, _DRIVER),
    lambda: sa_exc.InterfaceError(_STMT, {}, _DRIVER),
    lambda: sa_exc.DBAPIError(_STMT, {}, _DRIVER, connection_invalidated=True),
    lambda: redis_exc.ConnectionError("Error 111 connecting to redis:6379. Connection refused."),
    lambda: redis_exc.TimeoutError("Timeout reading from socket"),
    lambda: kombu_exc.OperationalError("Error 111 connecting to redis:6379. Connection refused."),
]

BUGS: list[Callable[[], BaseException]] = [
    lambda: sa_exc.DataError(_STMT, {}, _DRIVER),  # e.g. value out of int64 range
    lambda: sa_exc.IntegrityError(_STMT, {}, _DRIVER),
    lambda: sa_exc.ProgrammingError(_STMT, {}, _DRIVER),
    lambda: sa_exc.DBAPIError(_STMT, {}, _DRIVER),  # not connection-invalidated
    lambda: FileNotFoundError("nope"),  # an OSError, but not a connection failure
    lambda: RuntimeError("plain bug"),
]


def _app_raising(make_error: Callable[[], BaseException]) -> FastAPI:
    application = FastAPI()
    application.add_middleware(RequestContextMiddleware, settings=get_settings())
    register_exception_handlers(application)

    @application.get("/probe")
    async def probe() -> None:
        raise make_error()

    return application


async def _probe(
    make_error: Callable[[], BaseException], headers: dict[str, str] | None = None
) -> httpx.Response:
    transport = ASGITransport(app=_app_raising(make_error), raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/probe", headers=headers)


@pytest.mark.parametrize("make_error", UNREACHABLE)
async def test_unreachable_dependency_is_503_with_retry_after(
    make_error: Callable[[], BaseException],
) -> None:
    response = await _probe(make_error, {"X-Request-ID": "outage-1"})

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert response.headers["X-Request-ID"] == "outage-1"
    body = response.json()
    assert body["error"]["code"] == "service_unavailable"
    assert body["meta"]["request_id"] == "outage-1"
    assert "driver detail" not in response.text  # no internals leak


async def test_503_message_is_translated() -> None:
    response = await _probe(UNREACHABLE[0], {"Accept-Language": "id"})

    assert response.status_code == 503
    assert response.json()["error"]["message"].startswith("Layanan sedang tidak tersedia")


@pytest.mark.parametrize("make_error", BUGS)
async def test_bad_statement_or_plain_bug_stays_500(
    make_error: Callable[[], BaseException],
) -> None:
    response = await _probe(make_error)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "Retry-After" not in response.headers
    assert "driver detail" not in response.text


async def test_real_app_answers_503_when_the_database_is_unreachable(
    client: AsyncClient, tenant_id: str
) -> None:
    _, token, _ = await register_and_login(client, tenant_id, "outage@example.com")

    async def _db_down() -> AsyncGenerator[AsyncSession]:
        raise ConnectionRefusedError("connect call failed")
        yield  # pragma: no cover - makes this an async generator like the real dependency

    real_app.dependency_overrides[get_db_session] = _db_down
    try:
        response = await client.get("/api/v1/auth/me", headers=auth_header(token))
    finally:
        real_app.dependency_overrides.pop(get_db_session, None)

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert response.json()["error"]["code"] == "service_unavailable"


async def test_register_is_a_503_and_rolled_back_when_the_broker_is_unreachable(
    client: AsyncClient, tenant_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The welcome email can't be enqueued: the client gets a retryable 503 (not a 500), and the
    user row is rolled back so retrying the same registration works once the broker is back."""

    def _broker_down(*args: object, **kwargs: object) -> None:
        raise kombu_exc.OperationalError("Error 111 connecting to redis:6379. Connection refused.")

    body = {
        "tenantId": tenant_id,
        "email": "broker-down@example.com",
        "password": "S3curePass!",
        "fullName": "Broker Down",
    }
    with monkeypatch.context() as patch:
        patch.setattr(celery_app_module.celery_app, "send_task", _broker_down)
        down = await client.post("/api/v1/auth/register", json=body)
    assert down.status_code == 503, down.text
    assert down.json()["error"]["code"] == "service_unavailable"
    assert down.headers["Retry-After"] == "5"

    monkeypatch.setattr(celery_app_module.celery_app, "send_task", lambda *a, **k: None)
    again = await client.post("/api/v1/auth/register", json=body)
    assert again.status_code == 201, again.text
