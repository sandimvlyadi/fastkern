"""Production configuration, exercised in the real image.

The image is run with `ENVIRONMENT=production` and various bad settings; the app must refuse to
start with a clear message (before, `SECRET_KEY=change-me-...` -- the public value from
`.env.example` -- booted fine and forged admin tokens were accepted). A correctly configured
production container is then started to check what it exposes.
"""

import secrets
import shutil
import subprocess
import time
import uuid

import httpx
import pytest

from tests.e2e.conftest import ROOT

DOCKER = shutil.which("docker")
pytestmark = pytest.mark.skipif(DOCKER is None, reason="docker CLI not available")

STRONG_KEY = secrets.token_urlsafe(64)
PLACEHOLDER_KEY = "change-me-to-a-random-64-byte-value"
VALID_PRODUCTION = {
    "ENVIRONMENT": "production",
    "DEBUG": "false",
    "SECRET_KEY": STRONG_KEY,
    "CORS_ORIGINS": '["https://app.example.com"]',
    "EMAIL_BACKEND": "smtp",
    "SMTP_USERNAME": "mailer",
    "SMTP_PASSWORD": "not-a-real-password",
}
LOAD_SETTINGS = "from app.core.config import get_settings; get_settings(); print('settings-ok')"


def _run_in_api_image(
    args: list[str], env: dict[str, str], *, extra: list[str] | None = None
) -> subprocess.CompletedProcess[str]:
    assert DOCKER is not None
    command = [DOCKER, "compose", "run", "--rm", "--no-deps", *(extra or [])]
    for key, value in env.items():
        command += ["-e", f"{key}={value}"]
    return subprocess.run(
        [*command, "api", *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def _load_settings(**overrides: str) -> subprocess.CompletedProcess[str]:
    return _run_in_api_image(["python", "-c", LOAD_SETTINGS], {**VALID_PRODUCTION, **overrides})


def test_a_correct_production_configuration_starts() -> None:
    result = _load_settings()
    assert result.returncode == 0, result.stderr[-800:]
    assert "settings-ok" in result.stdout


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"SECRET_KEY": PLACEHOLDER_KEY}, "placeholder"),
        ({"SECRET_KEY": "a" * 64}, "distinct characters"),
        ({"SECRET_KEY": secrets.token_urlsafe(24)}, "at least 64"),
        ({"DEBUG": "true"}, "DEBUG must be false"),
        ({"CORS_ORIGINS": '["*"]'}, "CORS_ORIGINS"),
        ({"EMAIL_BACKEND": "console"}, "EMAIL_BACKEND=console"),
        ({"SMTP_PASSWORD": ""}, "SMTP_USERNAME and SMTP_PASSWORD"),
    ],
    ids=[
        "placeholder",
        "low-entropy",
        "short",
        "debug",
        "cors-wildcard",
        "console-email",
        "smtp-creds",
    ],
)
def test_an_unsafe_production_configuration_refuses_to_start(
    overrides: dict[str, str], expected: str
) -> None:
    result = _load_settings(**overrides)

    assert result.returncode != 0
    assert expected in result.stderr, result.stderr[-800:]
    if overrides.get("SECRET_KEY") in (PLACEHOLDER_KEY, "a" * 64):
        assert "secrets.token_urlsafe(64)" in result.stderr  # tells the operator how to fix it


def test_the_running_production_container_hides_the_schema_and_sets_hsts() -> None:
    assert DOCKER is not None
    name = f"e2e-prod-{uuid.uuid4().hex[:8]}"
    port = 18000 + secrets.randbelow(800)
    started = _run_in_api_image(
        ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"],  # noqa: S104 - in-container
        VALID_PRODUCTION,
        extra=["-d", "--name", name, "-p", f"{port}:8000"],
    )
    assert started.returncode == 0, started.stderr[-800:]
    try:
        base = f"http://localhost:{port}"
        deadline = time.time() + 60
        while True:
            try:
                if httpx.get(f"{base}/healthz", timeout=3).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            assert time.time() < deadline, "production container did not come up"
            time.sleep(1)

        for path in ("/docs", "/redoc", "/openapi.json"):
            assert httpx.get(f"{base}{path}", timeout=10).status_code == 404, path

        health = httpx.get(f"{base}/healthz", timeout=10)
        assert health.headers["strict-transport-security"].startswith("max-age=")
        assert health.headers["x-content-type-options"] == "nosniff"

        unauthenticated = httpx.get(f"{base}/api/v1/auth/me", timeout=10)
        assert unauthenticated.status_code in (401, 403)
        assert "traceback" not in unauthenticated.text.lower()
    finally:
        subprocess.run(
            [DOCKER, "rm", "-f", name], capture_output=True, text=True, timeout=60, check=False
        )
