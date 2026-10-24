"""Single-shot file upload lifecycle: upload -> poll to AVAILABLE -> metadata -> content ->
delete -> 404. RBAC and not-found handling for the metadata/content/delete endpoints.

Mirrors `test_clamav.py`'s upload/poll helpers (raw httpx, not `Api`, since multipart file
upload needs `files=`, which `Api.post`'s json-only signature doesn't support)."""

import time
import uuid

import httpx
import pytest

from tests.e2e.conftest import BASE_URL, Api, Tokens, expect

TERMINAL_STATES = {"AVAILABLE", "REJECTED", "FAILED"}


def _wait_for(check: object, what: str, timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if check():  # type: ignore[operator]
            return
        time.sleep(1)
    pytest.fail(f"timed out waiting for {what}")


def _upload(token: str, name: str, content: bytes) -> tuple[str, str]:
    response = httpx.post(
        f"{BASE_URL}/api/v1/files",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": (name, content, "text/plain")},
        timeout=60,
    )
    assert response.status_code == 202, response.text[:400]
    body = response.json()["data"]
    return str(body["uploadId"]), str(body["fileId"])


def _settled(api: Api, token: str, file_id: str) -> dict[str, object]:
    settled: dict[str, object] = {}

    def done() -> bool:
        body = api.get(f"/files/{file_id}", token).json()["data"]
        settled.update(body)
        return body["status"] in TERMINAL_STATES

    _wait_for(done, f"file {file_id} to leave processing", timeout=60)
    return settled


def test_upload_lifecycle_metadata_content_and_delete(api: Api, admin: Tokens) -> None:
    _, file_id = _upload(admin.access, "e2e-hello.txt", b"hello from the e2e suite\n")

    settled = _settled(api, admin.access, file_id)
    assert settled["status"] == "AVAILABLE"
    assert settled["failureReason"] is None

    content = httpx.get(
        f"{BASE_URL}/api/v1/files/{file_id}/content",
        headers={"Authorization": f"Bearer {admin.access}"},
        timeout=30,
    )
    assert content.status_code == 200
    assert content.content == b"hello from the e2e suite\n"

    expect(api.delete(f"/files/{file_id}", admin.access), 204)
    expect(api.get(f"/files/{file_id}", admin.access), 404, "file_not_found")


def test_upload_requires_files_write_permission(plain_user: Tokens) -> None:
    response = httpx.post(
        f"{BASE_URL}/api/v1/files",
        headers={"Authorization": f"Bearer {plain_user.access}"},
        files={"file": ("e2e-denied.txt", b"no permission", "text/plain")},
        timeout=30,
    )
    assert response.status_code == 403, response.text[:400]


def test_unknown_file_id_is_not_found(api: Api, admin: Tokens) -> None:
    missing = uuid.uuid4()
    expect(api.get(f"/files/{missing}", admin.access), 404, "file_not_found")
    expect(api.delete(f"/files/{missing}", admin.access), 404, "file_not_found")
