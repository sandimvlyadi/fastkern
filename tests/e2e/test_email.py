"""Welcome emails, end to end: API -> Celery task -> SMTP -> mailpit (the local mail catcher that
docker-compose points the api/worker at: SMTP host:1025, UI/API host:8025)."""

import json
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from tests.e2e.conftest import ROOT, SECRET_KEY, Account, setting

MAILPIT = os.environ.get("E2E_MAILPIT_URL", "http://localhost:8025").rstrip("/")


@pytest.fixture(scope="module", autouse=True)
def _mailpit() -> None:
    try:
        httpx.get(f"{MAILPIT}/api/v1/messages", timeout=5).raise_for_status()
    except httpx.HTTPError:
        pytest.skip(f"mailpit not reachable at {MAILPIT} (start it, or set E2E_MAILPIT_URL)")


def _wait_for_mail(address: str, timeout: float = 30) -> dict[str, Any]:
    """The (summary + full) mailpit message addressed to `address`, once it arrives."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = httpx.get(f"{MAILPIT}/api/v1/search", params={"query": f"to:{address}"}, timeout=10)
        messages = found.json().get("messages", [])
        if messages:
            summary = messages[0]
            full = httpx.get(f"{MAILPIT}/api/v1/message/{summary['ID']}", timeout=10).json()
            return {"summary": summary, "full": full}
        time.sleep(1)
    pytest.fail(f"no email for {address} within {timeout:.0f}s (is the worker running?)")


def test_registration_delivers_a_welcome_email(make_user: Callable[..., Account]) -> None:
    account = make_user(full_name="Ada Lovelace")

    mail = _wait_for_mail(account.email)

    summary, full = mail["summary"], mail["full"]
    assert [t["Address"] for t in summary["To"]] == [account.email]
    assert summary["From"]["Address"] == setting("EMAIL_FROM", "no-reply@example.com")
    assert summary["Subject"] == "Welcome!"
    assert "Ada Lovelace" in full["Text"]


def test_the_email_never_carries_the_jwt_signing_key(make_user: Callable[..., Account]) -> None:
    """The old task sent `SECRET_KEY` as a Bearer token to a third-party host."""
    if not SECRET_KEY:
        pytest.skip("SECRET_KEY unavailable")
    mail = _wait_for_mail(make_user().email)

    assert SECRET_KEY not in json.dumps(mail)


def test_a_recipient_with_an_indonesian_locale_gets_an_indonesian_email() -> None:
    """`RegisterRequest` has no locale field, so drive the task the way a producer would: by
    name over the broker, with the recipient's saved locale as an explicit argument."""
    from app.core.celery_app import celery_app

    address = f"id.{uuid.uuid4().hex[:8]}@example.com"
    celery_app.send_task(
        "notifications.send_welcome_email",
        kwargs={"to": address, "full_name": "Budi Santoso", "locale": "id"},
    )

    mail = _wait_for_mail(address)

    assert mail["summary"]["Subject"] == "Selamat datang!"
    assert "Budi Santoso" in mail["full"]["Text"]
    assert "akun Anda telah berhasil dibuat" in mail["full"]["Text"]


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available")
def test_worker_logs_show_deliveries_without_errors_or_secrets(
    make_user: Callable[..., Account],
) -> None:
    account = make_user()
    _wait_for_mail(account.email)
    docker = shutil.which("docker")
    assert docker is not None

    logs = subprocess.run(
        [docker, "compose", "logs", "--no-color", "--since", "2m", "worker"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout

    assert "welcome_email_sent" in logs
    assert "ConnectError" not in logs
    assert account.email not in logs  # only the masked address is ever logged
    if SECRET_KEY:
        assert SECRET_KEY not in logs
