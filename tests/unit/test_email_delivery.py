"""Email delivery: SMTP/console adapters, backend selection, config guards, task retries."""

import secrets
import smtplib
import socket
from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import SecretStr, ValidationError
from structlog.testing import capture_logs

from app.core.config import Environment, Settings
from app.modules.notifications.domain.exceptions import (
    NotificationDeliveryError,
    NotificationRejectedError,
)
from app.modules.notifications.domain.ports import NotificationSenderPort
from app.modules.notifications.infrastructure import smtp_adapter as smtp_module
from app.modules.notifications.infrastructure import tasks as notification_tasks
from app.modules.notifications.infrastructure.console_adapter import ConsoleEmailAdapter
from app.modules.notifications.infrastructure.email_adapter import HttpEmailAdapter
from app.modules.notifications.infrastructure.factory import build_notification_sender
from app.modules.notifications.infrastructure.masking import mask_email
from app.modules.notifications.infrastructure.smtp_adapter import SmtpEmailAdapter

PASSWORD = "smtp-password-that-must-not-leak"  # not a real credential
# Generated at runtime, not a literal: a hardcoded random-looking string here trips gitleaks'
# generic-api-key rule even though it's not a real secret.
STRONG_KEY = secrets.token_urlsafe(64)


def _settings(environment: Environment = Environment.LOCAL, **overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "environment": environment,
        "secret_key": STRONG_KEY,
        "debug": False,
        "database_url": "sqlite+aiosqlite:///./unit-test.db",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


# --- masking ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("jane.doe@example.com", "j***@example.com"),
        ("a@b.io", "a***@b.io"),
        ("no-at-sign", "***"),
        ("@example.com", "***"),
        ("user@", "***"),
        ("", "***"),
    ],
)
def test_mask_email(address: str, expected: str) -> None:
    assert mask_email(address) == expected


# --- console ---------------------------------------------------------------------------------


async def test_console_adapter_logs_masked_recipient_and_subject_but_never_the_body() -> None:
    with capture_logs() as logs:
        await ConsoleEmailAdapter().send_email(
            to="jane.doe@example.com", subject="Welcome!", body="secret-link-token-123"
        )

    (entry,) = [log for log in logs if log["event"] == "email_console_delivery"]
    assert entry["to"] == "j***@example.com"
    assert entry["subject"] == "Welcome!"
    assert "secret-link-token-123" not in str(logs)
    assert "jane.doe" not in str(logs)


# --- SMTP ------------------------------------------------------------------------------------


class _FakeSmtp:
    """Stands in for smtplib.SMTP / SMTP_SSL; records the conversation."""

    instances: ClassVar[list[_FakeSmtp]] = []
    fail_with: ClassVar[BaseException | None] = None

    def __init__(self, host: str, port: int, **kwargs: Any) -> None:
        self.host, self.port, self.kwargs = host, port, kwargs
        self.calls: list[tuple[str, Any]] = []
        _FakeSmtp.instances.append(self)

    def __enter__(self) -> _FakeSmtp:
        return self

    def __exit__(self, *_: object) -> None:
        self.calls.append(("quit", None))

    def starttls(self, *, context: object) -> None:
        self.calls.append(("starttls", context))

    def login(self, username: str, password: str) -> None:
        self.calls.append(("login", (username, password)))

    def send_message(self, message: Any) -> None:
        if _FakeSmtp.fail_with is not None:
            raise _FakeSmtp.fail_with
        self.calls.append(("send_message", message))


@pytest.fixture(autouse=True)
def _fake_smtplib(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeSmtp.instances = []
    _FakeSmtp.fail_with = None
    monkeypatch.setattr(smtp_module.smtplib, "SMTP", _FakeSmtp)
    monkeypatch.setattr(smtp_module.smtplib, "SMTP_SSL", _FakeSmtp)


def _smtp(**overrides: Any) -> SmtpEmailAdapter:
    kwargs: dict[str, Any] = {
        "host": "mail.test",
        "port": 1025,
        "sender": "no-reply@app.test",
    }
    kwargs.update(overrides)
    return SmtpEmailAdapter(**kwargs)


async def test_smtp_sends_a_well_formed_message() -> None:
    await _smtp().send_email(to="jane@example.com", subject="Hello", body="Hi Jane")

    (server,) = _FakeSmtp.instances
    assert (server.host, server.port) == ("mail.test", 1025)
    assert server.kwargs["timeout"] == 10.0
    (message,) = [arg for name, arg in server.calls if name == "send_message"]
    assert message["From"] == "no-reply@app.test"
    assert message["To"] == "jane@example.com"
    assert message["Subject"] == "Hello"
    assert message.get_content().strip() == "Hi Jane"
    assert [name for name, _ in server.calls] == ["send_message", "quit"]  # no TLS / auth asked


async def test_smtp_handles_non_ascii_subject_and_body() -> None:
    await _smtp().send_email(to="a@example.com", subject="Selamat datang, Budi ✓", body="Halo ✓")

    (message,) = [arg for name, arg in _FakeSmtp.instances[0].calls if name == "send_message"]
    assert message["Subject"] == "Selamat datang, Budi ✓"
    assert message.get_content().strip() == "Halo ✓"


async def test_smtp_starttls_then_login_when_configured() -> None:
    adapter = _smtp(starttls=True, username="mailer", password=PASSWORD)

    await adapter.send_email(to="a@example.com", subject="s", body="b")

    names = [name for name, _ in _FakeSmtp.instances[0].calls]
    assert names == ["starttls", "login", "send_message", "quit"]
    assert dict(_FakeSmtp.instances[0].calls)["login"] == ("mailer", PASSWORD)


async def test_smtp_ssl_uses_implicit_tls_and_no_starttls() -> None:
    await _smtp(use_ssl=True, timeout_seconds=3.0).send_email(to="a@e.io", subject="s", body="b")

    (server,) = _FakeSmtp.instances
    assert "context" in server.kwargs  # SMTP_SSL got a verifying SSL context
    assert server.kwargs["timeout"] == 3.0
    assert "starttls" not in [name for name, _ in server.calls]


async def test_smtp_skips_login_without_both_username_and_password() -> None:
    await _smtp(username="mailer").send_email(to="a@example.com", subject="s", body="b")

    assert "login" not in [name for name, _ in _FakeSmtp.instances[0].calls]


TRANSIENT = [
    smtplib.SMTPServerDisconnected("server closed the connection"),
    smtplib.SMTPResponseException(451, b"try again later"),
    smtplib.SMTPConnectError(421, b"service not available"),
    smtplib.SMTPRecipientsRefused({"a@example.com": (450, b"mailbox busy")}),
    ConnectionRefusedError(111, "Connection refused"),
    ConnectionResetError(104, "reset"),
    socket.gaierror(-2, "Name or service not known"),
    TimeoutError("timed out"),
    OSError("network unreachable"),
]

PERMANENT = [
    smtplib.SMTPResponseException(550, b"no such user"),
    smtplib.SMTPAuthenticationError(535, b"authentication failed"),
    smtplib.SMTPSenderRefused(553, b"sender not allowed", "no-reply@app.test"),
    smtplib.SMTPRecipientsRefused({"a@example.com": (550, b"no such mailbox")}),
    smtplib.SMTPNotSupportedError("STARTTLS extension not supported by server"),
    smtplib.SMTPException("something else went wrong in the protocol"),
]


@pytest.mark.parametrize("error", TRANSIENT, ids=lambda e: type(e).__name__)
async def test_smtp_transient_failures_become_retryable_delivery_errors(
    error: BaseException,
) -> None:
    _FakeSmtp.fail_with = error

    with pytest.raises(NotificationDeliveryError) as raised:
        await _smtp().send_email(to="a@example.com", subject="s", body="b")

    # Type and args, not identity: asyncio.to_thread rebuilds a TimeoutError as a new instance.
    cause = raised.value.__cause__
    assert type(cause) is type(error)
    assert cause is not None and cause.args == error.args


@pytest.mark.parametrize("error", PERMANENT, ids=lambda e: type(e).__name__)
async def test_smtp_permanent_failures_are_rejections_that_are_not_retried(
    error: BaseException,
) -> None:
    _FakeSmtp.fail_with = error

    with pytest.raises(NotificationRejectedError) as raised:
        await _smtp().send_email(to="a@example.com", subject="s", body="b")

    assert not isinstance(raised.value, NotificationDeliveryError)  # the task retries only those


async def test_smtp_failure_logs_neither_the_password_nor_the_body_nor_the_full_address() -> None:
    _FakeSmtp.fail_with = smtplib.SMTPAuthenticationError(535, PASSWORD.encode())
    adapter = _smtp(username="mailer", password=PASSWORD)

    with capture_logs() as logs, pytest.raises(NotificationRejectedError):
        await adapter.send_email(
            to="jane.doe@example.com", subject="s", body="confirmation-link-abc123"
        )

    (entry,) = [log for log in logs if log["event"] == "email_delivery_failed"]
    assert entry["to"] == "j***@example.com"
    assert entry["transient"] is False
    text = str(logs)
    assert PASSWORD not in text
    assert "confirmation-link-abc123" not in text
    assert "jane.doe" not in text


# --- backend selection -----------------------------------------------------------------------


def test_factory_builds_the_console_adapter_by_default() -> None:
    assert isinstance(build_notification_sender(_settings()), ConsoleEmailAdapter)


def test_factory_builds_a_configured_smtp_adapter() -> None:
    settings = _settings(
        email_backend="smtp",
        email_from="hello@app.test",
        smtp_host="smtp.provider.test",
        smtp_port=587,
        smtp_username="mailer",
        smtp_password=SecretStr(PASSWORD),
        smtp_starttls=True,
        smtp_timeout_seconds=4.5,
    )

    sender = build_notification_sender(settings)

    assert isinstance(sender, SmtpEmailAdapter)
    assert (sender._host, sender._port, sender._sender) == (
        "smtp.provider.test",
        587,
        "hello@app.test",
    )
    assert (sender._username, sender._password) == ("mailer", PASSWORD)
    assert (sender._starttls, sender._use_ssl, sender._timeout) == (True, False, 4.5)


def test_factory_builds_the_http_adapter_from_settings_never_from_secret_key() -> None:
    settings = _settings(
        email_backend="http",
        email_http_base_url="https://mail.provider.test",
        email_http_api_key=SecretStr("provider-key"),
    )

    sender = build_notification_sender(settings)

    assert isinstance(sender, HttpEmailAdapter)
    assert sender._base_url == "https://mail.provider.test"
    assert sender._api_key == "provider-key"
    assert sender._api_key != settings.secret_key


def test_factory_refuses_an_http_backend_without_its_configuration() -> None:
    with pytest.raises(ValueError, match="EMAIL_HTTP_BASE_URL"):
        build_notification_sender(_settings(email_backend="http"))


def test_the_notifications_module_never_touches_the_jwt_signing_key() -> None:
    # The old task used `settings.secret_key` as a Bearer token for a third-party host.
    module_dir = Path(smtp_module.__file__).resolve().parents[1]
    offenders = [
        str(path.relative_to(module_dir))
        for path in module_dir.rglob("*.py")
        if "secret_key" in path.read_text()
    ]
    assert offenders == []


# --- config guards ---------------------------------------------------------------------------


def _production(**overrides: object) -> Settings:
    return _settings(Environment.PRODUCTION, **overrides)


def test_production_refuses_the_console_backend() -> None:
    with pytest.raises(ValidationError, match="EMAIL_BACKEND=console"):
        _production(email_backend="console")


def test_production_smtp_needs_credentials() -> None:
    with pytest.raises(ValidationError, match="SMTP_USERNAME and SMTP_PASSWORD"):
        _production(email_backend="smtp", smtp_username="mailer")
    with pytest.raises(ValidationError, match="SMTP_USERNAME and SMTP_PASSWORD"):
        _production(email_backend="smtp", smtp_password=SecretStr(PASSWORD))


def test_production_http_needs_base_url_and_api_key() -> None:
    with pytest.raises(ValidationError, match="EMAIL_HTTP_BASE_URL and EMAIL_HTTP_API_KEY"):
        _production(email_backend="http", email_http_base_url="https://m.test")
    settings = _production(
        email_backend="http",
        email_http_base_url="https://m.test",
        email_http_api_key=SecretStr("k"),
    )
    assert settings.email_backend == "http"


def test_production_accepts_a_complete_smtp_setup() -> None:
    settings = _production(
        email_backend="smtp", smtp_username="mailer", smtp_password=SecretStr(PASSWORD)
    )
    assert settings.smtp_password is not None
    assert PASSWORD not in repr(settings)  # SecretStr: not exposed in reprs/logs


@pytest.mark.parametrize("environment", [Environment.LOCAL, Environment.TEST, Environment.STAGING])
def test_non_production_may_use_the_console_backend(environment: Environment) -> None:
    assert _settings(environment).email_backend == "console"


def test_starttls_and_ssl_are_mutually_exclusive_everywhere() -> None:
    with pytest.raises(ValidationError, match="mutually exclusive"):
        _settings(smtp_starttls=True, smtp_ssl=True)


@pytest.mark.parametrize(
    "bad", [{"smtp_port": 0}, {"smtp_port": 70000}, {"smtp_timeout_seconds": 0}]
)
def test_smtp_numeric_settings_are_validated(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _settings(**bad)


# --- task: retry policy ----------------------------------------------------------------------


class _FlakySender(NotificationSenderPort):
    def __init__(self, failures: list[Exception]) -> None:
        self.failures = list(failures)
        self.attempts = 0
        self.delivered: list[str] = []

    async def send_email(self, *, to: str, subject: str, body: str) -> None:
        self.attempts += 1
        if self.failures:
            raise self.failures.pop(0)
        self.delivered.append(to)


def _run_task(monkeypatch: pytest.MonkeyPatch, sender: NotificationSenderPort) -> Any:
    monkeypatch.setattr(notification_tasks, "build_notification_sender", lambda settings: sender)
    return notification_tasks.send_welcome_email_task.apply(
        kwargs={"to": "a@example.com", "full_name": "Ada", "locale": "en"}
    )


def test_task_declares_the_retry_policy() -> None:
    task = notification_tasks.send_welcome_email_task
    assert task.autoretry_for == (NotificationDeliveryError,)
    assert task.max_retries == 3
    assert task.retry_backoff is True
    assert task.retry_backoff_max == 300
    assert task.retry_jitter is True
    assert task.acks_late is True


def test_task_retries_a_transient_failure_and_then_delivers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender = _FlakySender([NotificationDeliveryError(), NotificationDeliveryError()])

    result = _run_task(monkeypatch, sender)

    assert result.successful(), result.traceback
    assert sender.attempts == 3
    assert sender.delivered == ["a@example.com"]


def test_task_gives_up_after_max_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    sender = _FlakySender([NotificationDeliveryError() for _ in range(10)])

    result = _run_task(monkeypatch, sender)

    assert result.failed()
    assert isinstance(result.result, NotificationDeliveryError)
    assert sender.attempts == 4  # the first try + max_retries=3
    assert sender.delivered == []


def test_task_does_not_retry_a_permanent_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    sender = _FlakySender([NotificationRejectedError()])

    result = _run_task(monkeypatch, sender)

    assert result.failed()
    assert isinstance(result.result, NotificationRejectedError)
    assert sender.attempts == 1


def test_task_does_not_retry_a_programming_error(monkeypatch: pytest.MonkeyPatch) -> None:
    sender = _FlakySender([RuntimeError("bug")])

    result = _run_task(monkeypatch, sender)

    assert result.failed()
    assert sender.attempts == 1


def test_task_logs_success_with_a_masked_recipient(monkeypatch: pytest.MonkeyPatch) -> None:
    sender = _FlakySender([])

    with capture_logs() as logs:
        _run_task(monkeypatch, sender)

    (entry,) = [log for log in logs if log["event"] == "welcome_email_sent"]
    assert entry["to"] == "a***@example.com"
