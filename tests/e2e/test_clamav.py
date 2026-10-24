"""Malware scanning against a REAL clamd (`make e2e-clamav`, CI job `docker-e2e-clamav`).

Needs the stack started with `--profile clamav` and MALWARE_SCAN_BACKEND=clamav. Excluded from
`make e2e`, which runs on stacks without ClamAV.
"""

import time

import httpx
import pytest

from tests.e2e.conftest import BASE_URL, Api, Tokens

pytestmark = pytest.mark.e2e_clamav

# The standard antivirus test string (EICAR). Harmless, and every clamd build flags it. It is
# assembled here so no scanner sees the whole literal in the source.
EICAR = b"X5O!P%@AP[4" + b"\\" + b"PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
TERMINAL_STATES = {"AVAILABLE", "REJECTED", "FAILED"}


def _wait_for(check: object, what: str, timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if check():  # type: ignore[operator]
                return
        except httpx.HTTPError:
            pass
        time.sleep(5)
    pytest.fail(f"timed out waiting for {what}")


def _wait_for_clamav() -> None:
    # `clamav` only appears in /readyz when the stack runs MALWARE_SCAN_BACKEND=clamav. Fail now,
    # with the real cause, instead of waiting for a status that can never arrive.
    checks = httpx.get(f"{BASE_URL}/readyz", timeout=15).json()["checks"]
    if "clamav" not in checks:
        pytest.fail(
            "the stack has no clamav check: start it with MALWARE_SCAN_BACKEND=clamav "
            "and the clamav compose profile"
        )

    # clamd loads its signature database after the container starts, and the first heartbeat that
    # reaches it only runs one interval later, so allow for both.
    def clamav_ok() -> bool:
        return httpx.get(f"{BASE_URL}/readyz", timeout=15).json()["checks"].get("clamav") == "ok"

    _wait_for(clamav_ok, "/readyz to report clamav ok", timeout=600)


def _upload(token: str, name: str, content: bytes) -> str:
    response = httpx.post(
        f"{BASE_URL}/api/v1/files",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": (name, content, "text/plain")},
        timeout=60,
    )
    assert response.status_code == 202, response.text[:400]
    return str(response.json()["data"]["fileId"])


def _settled(api: Api, token: str, file_id: str) -> dict[str, object]:
    settled: dict[str, object] = {}

    def done() -> bool:
        body = api.get(f"/files/{file_id}", token).json()["data"]
        settled.update(body)
        return body["status"] in TERMINAL_STATES

    _wait_for(done, f"file {file_id} to leave processing", timeout=120)
    return settled


def test_eicar_is_rejected_as_malware(api: Api, superadmin: Tokens) -> None:
    _wait_for_clamav()
    file_id = _upload(superadmin.access, "eicar.txt", EICAR)

    body = _settled(api, superadmin.access, file_id)

    assert body["status"] == "REJECTED"
    assert body["failureReason"] == "MALWARE"


def test_clean_file_is_available(api: Api, superadmin: Tokens) -> None:
    _wait_for_clamav()
    file_id = _upload(superadmin.access, "clean.txt", b"hello from the e2e suite\n")

    body = _settled(api, superadmin.access, file_id)

    assert body["status"] == "AVAILABLE"
    assert body["failureReason"] is None
