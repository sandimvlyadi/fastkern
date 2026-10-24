from collections.abc import Callable

import httpx
import pytest
import tenacity
from structlog.testing import capture_logs

from app.modules.notifications.application.use_cases import SendWelcomeEmailUseCase
from app.modules.notifications.domain.exceptions import (
    NotificationDeliveryError,
    NotificationRejectedError,
)
from app.modules.notifications.infrastructure import email_adapter
from app.modules.notifications.infrastructure import tasks as notification_tasks
from app.modules.notifications.infrastructure.email_adapter import HttpEmailAdapter
from tests.fakes import RecordingNotificationSender


@pytest.fixture(autouse=True)
def _no_retry_backoff() -> None:
    """Retries in these tests are triggered on purpose; skip the real
    exponential backoff so they don't add seconds to the suite."""
    HttpEmailAdapter._post.retry.wait = tenacity.wait_none()


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _adapter(handler: Callable[[httpx.Request], httpx.Response]) -> HttpEmailAdapter:
    return HttpEmailAdapter(base_url="https://provider.test", api_key="k", client=_client(handler))


async def test_send_welcome_email_use_case_builds_expected_message() -> None:
    sender = RecordingNotificationSender()
    await SendWelcomeEmailUseCase(sender).execute(to="a@example.com", full_name="Ada")

    assert sender.sent == [
        {
            "to": "a@example.com",
            "subject": "Welcome!",
            "body": "Hi Ada, your account has been created.",
        }
    ]


async def test_http_email_adapter_success() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    adapter = HttpEmailAdapter(
        base_url="https://provider.test", api_key="secret", client=_client(handler)
    )
    await adapter.send_email(to="a@example.com", subject="Hi", body="body")

    assert len(requests) == 1
    request = requests[0]
    assert request.url.path == "/v1/email"
    assert request.headers["authorization"] == "Bearer secret"


async def test_http_email_adapter_4xx_fails_fast() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, text="bad request")

    adapter = _adapter(handler)
    # A permanent rejection: NOT a NotificationDeliveryError, so the task won't retry it.
    with pytest.raises(NotificationRejectedError) as raised:
        await adapter.send_email(to="a@example.com", subject="Hi", body="body")
    assert not isinstance(raised.value, NotificationDeliveryError)
    assert calls == 1


async def test_http_email_adapter_retries_then_succeeds() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 2:
            return httpx.Response(503)
        return httpx.Response(200)

    adapter = _adapter(handler)
    await adapter.send_email(to="a@example.com", subject="Hi", body="body")
    assert calls == 2


async def test_http_email_adapter_exhausts_retries_and_raises() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    adapter = _adapter(handler)
    with pytest.raises(NotificationDeliveryError):
        await adapter.send_email(to="a@example.com", subject="Hi", body="body")
    assert calls == 3


async def test_http_email_adapter_timeout_retried_and_raises() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timeout", request=request)

    adapter = _adapter(handler)
    with pytest.raises(NotificationDeliveryError):
        await adapter.send_email(to="a@example.com", subject="Hi", body="body")
    assert calls == 3


@pytest.mark.parametrize(
    "make_error",
    [
        lambda request: httpx.ConnectError("Name or service not known", request=request),
        lambda request: httpx.ReadError("reset", request=request),
        lambda request: httpx.ConnectTimeout("t/o", request=request),
    ],
)
async def test_http_email_adapter_retries_every_transport_failure(
    make_error: Callable[[httpx.Request], Exception],
) -> None:
    """`ConnectError` used to escape unretried (only TimeoutException was caught)."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise make_error(request)

    adapter = _adapter(handler)
    with pytest.raises(NotificationDeliveryError):
        await adapter.send_email(to="a@example.com", subject="Hi", body="body")
    assert calls == 3


async def test_http_email_adapter_treats_429_as_transient() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429) if calls < 2 else httpx.Response(200)

    await _adapter(handler).send_email(to="a@example.com", subject="Hi", body="body")
    assert calls == 2


async def test_http_email_adapter_does_not_log_the_provider_response_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, text="rejected: body was 'super secret content'")

    with capture_logs() as logs, pytest.raises(NotificationRejectedError):
        await _adapter(handler).send_email(to="a@example.com", subject="Hi", body="b")

    assert [log["event"] for log in logs] == ["email_delivery_rejected"]  # it did log...
    assert logs[0]["status"] == 422
    assert "super secret" not in str(logs)  # ...but only the status, not the provider's body


async def test_http_email_adapter_opens_and_closes_its_own_client_when_none_injected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[httpx.Request] = []
    opened: list[httpx.AsyncClient] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    real_client_cls = httpx.AsyncClient  # captured first: the patch below replaces it globally

    def _make_client(**_: object) -> httpx.AsyncClient:
        client = real_client_cls(transport=httpx.MockTransport(handler))
        opened.append(client)
        return client

    monkeypatch.setattr(email_adapter.httpx, "AsyncClient", _make_client)

    await HttpEmailAdapter(base_url="https://provider.test", api_key="k").send_email(
        to="a@example.com", subject="Hi", body="b"
    )

    assert len(seen) == 1
    assert opened[0].is_closed  # no leaked client / pool


def test_send_welcome_email_task_sends_through_the_configured_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender = RecordingNotificationSender()
    monkeypatch.setattr(notification_tasks, "build_notification_sender", lambda settings: sender)

    notification_tasks.send_welcome_email_task(to="a@example.com", full_name="Ada")

    assert [m["to"] for m in sender.sent] == ["a@example.com"]
    assert "Ada" in sender.sent[0]["body"]


def test_welcome_email_task_locale_reaches_the_message(monkeypatch: pytest.MonkeyPatch) -> None:
    sender = RecordingNotificationSender()
    monkeypatch.setattr(notification_tasks, "build_notification_sender", lambda settings: sender)

    notification_tasks.send_welcome_email_task(to="a@example.com", full_name="Ada", locale="id")

    assert sender.sent[0]["subject"] != "Welcome!"  # translated, not the English default
