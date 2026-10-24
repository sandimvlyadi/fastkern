"""Dependency outages against the real stack (`make e2e-destructive`).

STOPS AND RESTARTS the redis and db containers. A dead dependency must be a clean 503 +
Retry-After (never a 500 with a traceback), the liveness probe must keep answering, `/readyz`
must say which dependency is down, and the API must recover on its own once it is back.
"""

import shutil
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import httpx
import pytest

from tests.e2e.conftest import (
    BASE_URL,
    ROOT,
    STRONG_PASSWORD,
    TENANT_ID,
    Api,
    Tokens,
    data,
    expect,
)

pytestmark = [
    pytest.mark.e2e_destructive,
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available"),
]

DOCKER = shutil.which("docker") or "docker"


def _compose(*args: str) -> None:
    subprocess.run(
        [DOCKER, "compose", *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=True,
        timeout=180,
    )


def _readyz() -> httpx.Response:
    return httpx.get(f"{BASE_URL}/readyz", timeout=15)


def _wait_for(check: Callable[[], bool], what: str, timeout: float = 120) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if check():
                return
        except httpx.HTTPError:
            pass
        time.sleep(2)
    pytest.fail(f"timed out waiting for {what}")


@contextmanager
def _stopped(service: str) -> Iterator[None]:
    _compose("stop", service)
    try:
        yield
    finally:
        _compose("start", service)
        _wait_for(lambda: _readyz().status_code == 200, f"/readyz to recover after `{service}`")


def test_redis_outage_degrades_gracefully_and_recovers(
    api: Api, superadmin: Tokens, run_id: str
) -> None:
    token = api.tokens("user@example.com").access
    partner_key = data(
        api.post(
            "/api-clients",
            superadmin.access,
            json={"name": f"e2e-outage-{run_id}", "scopes": ["posts:read"]},
        )
    )["plaintextKey"]
    registration = {
        "tenantId": TENANT_ID,
        "email": f"outage.{run_id}@example.com",
        "password": STRONG_PASSWORD,
        "fullName": "Outage Registration",
    }

    with _stopped("redis"):
        _wait_for(lambda: _readyz().status_code == 503, "/readyz to notice redis is down")
        assert _readyz().json()["checks"]["redis"] != "ok"
        assert (
            httpx.get(f"{BASE_URL}/healthz", timeout=10).status_code == 200
        )  # liveness unaffected

        # Login needs Redis (the refresh-token store): a clean, retryable 503.
        down = expect(api.login("user@example.com"), 503, "service_unavailable")
        assert down.headers["Retry-After"] == "5"
        assert "traceback" not in down.text.lower()
        # ...but an existing access token keeps working: the session denylist and the rate
        # limiter both fail OPEN rather than take the API down.
        assert api.get("/auth/me", token).status_code == 200
        # Redis is also the Celery broker. Registering can't enqueue its welcome email: a clean,
        # retryable 503 that rolls the new user back, not a 500.
        down = expect(api.post("/auth/register", json=registration), 503, "service_unavailable")
        assert down.headers["Retry-After"] == "5"
        # A partner call only loses its (best-effort) traffic-log row; the call itself succeeds.
        assert api.get("/partners/posts", headers={"X-API-Key": partner_key}).status_code == 200

    expect(api.login("user@example.com"), 200)  # recovered without a restart
    expect(api.post("/auth/register", json=registration), 201)  # nothing half-created


def test_database_outage_is_a_503_and_recovers(api: Api) -> None:
    token = api.tokens("user@example.com").access

    with _stopped("db"):
        _wait_for(lambda: _readyz().status_code == 503, "/readyz to notice the database is down")
        assert _readyz().json()["checks"]["database"] != "ok"
        assert httpx.get(f"{BASE_URL}/healthz", timeout=10).status_code == 200

        for response in (
            api.login("user@example.com"),
            api.get("/auth/me", token),
            api.get("/users", token),
        ):
            expect(response, 503, "service_unavailable")
            assert response.headers["Retry-After"] == "5"
            assert "traceback" not in response.text.lower()

    expect(api.login("user@example.com"), 200)  # the connection pool heals itself


def test_beat_outage_is_reported_but_keeps_the_api_in_rotation(api: Api) -> None:
    # The heartbeat key expires after three missed intervals (180 s at the default 60 s).
    with _stopped("beat"):
        _wait_for(
            lambda: _readyz().json()["checks"].get("beat") == "error",
            "/readyz to notice beat is down",
            timeout=300,
        )
        response = _readyz()
        assert response.status_code == 200  # a dead scheduler must not pull the API out of rotation
        assert response.json()["status"] == "ready"
        expect(api.login("user@example.com"), 200)
