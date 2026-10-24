"""Platform surface: probes, metrics, docs, envelope on unknown routes, request ids, CORS and
security headers, OpenAPI quality, and the containers' own health."""

import json
import shutil
import subprocess
import time

import httpx
import pytest

from tests.e2e.conftest import _DOTENV, BASE_URL, ROOT


def _get(path: str, **kwargs: object) -> httpx.Response:
    return httpx.get(f"{BASE_URL}{path}", timeout=20, **kwargs)  # type: ignore[arg-type]


def test_healthz_is_ok() -> None:
    response = _get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_reports_every_dependency() -> None:
    # beat's first heartbeat only fires one interval after the stack starts, so wait for it.
    deadline = time.time() + 150
    while time.time() < deadline:
        body = _get("/readyz").json()
        if body["checks"].get("beat") == "ok":
            break
        time.sleep(5)
    response = _get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"] == {"database": "ok", "redis": "ok", "celery": "ok", "beat": "ok"}


def test_metrics_are_scrapeable_and_include_the_denylist_counter() -> None:
    response = _get("/metrics")
    assert response.status_code == 200  # METRICS_AUTH_TOKEN is unset in the compose stack
    assert "http_request" in response.text
    assert "session_denylist_backend_errors_total" in response.text


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_docs_are_served_outside_production(path: str) -> None:
    assert _get(path).status_code == 200


def test_unknown_route_is_a_404_in_the_standard_envelope() -> None:
    response = _get("/definitely-not-a-route")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "http_error"
    assert "request_id" in response.json()["meta"]


def test_request_id_is_echoed_and_generated() -> None:
    echoed = _get("/healthz", headers={"X-Request-ID": "e2e-req-123"})
    assert echoed.headers["x-request-id"] == "e2e-req-123"
    assert _get("/healthz").headers["x-request-id"]


def test_error_bodies_carry_the_request_id_of_the_header() -> None:
    response = _get("/definitely-not-a-route", headers={"X-Request-ID": "e2e-err-1"})
    assert response.headers["x-request-id"] == "e2e-err-1"
    assert response.json()["meta"]["request_id"] == "e2e-err-1"


def test_cors_allows_the_configured_origin_and_only_that() -> None:
    allowed = "http://localhost:3000"
    if allowed not in _DOTENV.get("CORS_ORIGINS", allowed):
        pytest.skip(f"{allowed} is not in this stack's CORS_ORIGINS")
    ok = httpx.options(
        f"{BASE_URL}/api/v1/users",
        headers={
            "Origin": allowed,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert ok.headers.get("access-control-allow-origin") == allowed
    denied = httpx.options(
        f"{BASE_URL}/api/v1/users",
        headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in denied.headers


def test_security_headers_are_present_and_hsts_only_in_production() -> None:
    probe = _get("/healthz")
    api = _get("/api/v1/auth/me")
    for response in (probe, api):
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "strict-transport-security" not in response.headers  # ENVIRONMENT=local
    assert api.headers["cache-control"] == "no-store"
    assert "cache-control" not in probe.headers


def test_openapi_documents_an_example_for_every_request_schema() -> None:
    schemas = _get("/openapi.json").json()["components"]["schemas"]
    request_schemas = {name: body for name, body in schemas.items() if name.endswith("Request")}
    assert len(request_schemas) >= 10
    missing = [name for name, body in request_schemas.items() if not body.get("examples")]
    assert missing == []


def test_openapi_documents_the_activate_endpoint_and_the_new_errors() -> None:
    spec = _get("/openapi.json").json()
    assert "/api/v1/users/{user_id}/activate" in spec["paths"]
    deactivate = spec["paths"]["/api/v1/users/{user_id}/deactivate"]["post"]["responses"]
    assert "cannot_modify_self" in json.dumps(deactivate["409"])


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available")
def test_every_container_reports_healthy() -> None:
    """The worker used to be permanently `unhealthy`: the image's HTTP healthcheck was inherited
    by a process that serves no HTTP."""
    docker = shutil.which("docker")
    assert docker is not None
    output = subprocess.run(
        [docker, "compose", "ps", "--format", "json"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout
    services = {}
    for line in output.splitlines():
        if line.strip():
            item = json.loads(line)
            services[item["Service"]] = item.get("Health", "")
    assert {"api", "db", "redis", "worker"} <= set(services)
    unhealthy = {name: health for name, health in services.items() if health != "healthy"}
    assert unhealthy == {}
