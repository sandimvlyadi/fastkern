"""End-to-end HTTP tests for the files module (SQLite test DB, Redis for auth, temp local storage).

Celery dispatch is captured instead of sent to a broker. The worker pipeline itself is run in
`test_files_pipeline.py`.
"""

import hashlib
import io
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from httpx import AsyncClient
from PIL import Image
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.celery_app import celery_app
from app.core.config import get_settings
from app.main import app
from app.modules.files.application.pipeline import ProcessUploadUseCase
from app.modules.files.infrastructure.factory import (
    build_image_options,
    build_image_processor,
    build_malware_scanner,
)
from app.modules.files.infrastructure.models import FileUploadModel
from app.modules.files.infrastructure.repository import (
    ChunkRepository,
    FileRepository,
    UploadRepository,
)
from app.modules.files.infrastructure.storage.local_fs import LocalFileSystemStorage
from app.modules.files.presentation.router import get_object_storage
from tests.helpers import auth_header, grant_role, register_and_login

BASE = "/api/v1"
_UPLOAD_PERMS = ["files:read", "files:write", "files:delete"]


@pytest.fixture
def storage_root(tmp_path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    root = tmp_path / "data"
    app.dependency_overrides[get_object_storage] = lambda: LocalFileSystemStorage(root)
    dispatched: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        celery_app,
        "send_task",
        lambda name, kwargs=None, **_: dispatched.append((name, kwargs or {})),
    )
    return root, dispatched


def _png(size: tuple[int, int] = (40, 30)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, "teal").save(buffer, "PNG")
    return buffer.getvalue()


def _slices(data: bytes, size: int) -> list[bytes]:
    return [data[i : i + size] for i in range(0, len(data), size)]


async def _user(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    email: str,
    permissions: list[str] = _UPLOAD_PERMS,
) -> tuple[str, str]:
    user_id, token, _ = await register_and_login(client, tenant_id, email)
    if permissions:
        await grant_role(
            session_factory,
            tenant_id=tenant_id,
            user_id=user_id,
            role_name=f"files-{email}",
            permissions=permissions,
        )
    return user_id, token


async def _initiate(
    client: AsyncClient,
    token: str,
    *,
    size: int,
    chunk_size: int,
    visibility: str = "PRIVATE",
    filename: str = "photo.png",
) -> dict[str, Any]:
    response = await client.post(
        f"{BASE}/files/uploads",
        json={
            "filename": filename,
            "size": size,
            "chunkSize": chunk_size,
            "visibility": visibility,
        },
        headers=auth_header(token),
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def _put_chunk(
    client: AsyncClient,
    token: str,
    upload_id: str,
    index: int,
    body: bytes,
    sha: str | None = None,
):  # type: ignore[no-untyped-def]
    headers = {**auth_header(token), "Content-Type": "application/octet-stream"}
    if sha is not None:
        headers["X-Chunk-SHA256"] = sha
    return await client.put(
        f"{BASE}/files/uploads/{upload_id}/chunks/{index}", content=body, headers=headers
    )


async def test_upload_out_of_order_resume_and_complete(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    storage_root,  # type: ignore[no-untyped-def]
) -> None:
    _, dispatched = storage_root
    _, token = await _user(client, tenant_id, session_factory, "uploader@example.com")
    data = _png()
    chunks = _slices(data, 64)
    session = await _initiate(
        client, token, size=len(data), chunk_size=64, visibility="PUBLIC", filename="../evil/x.png"
    )
    upload_id = session["uploadId"]
    assert session["totalChunks"] == len(chunks)

    # Out of order, with a checksum header that must match.
    last = len(chunks) - 1
    ok = await _put_chunk(
        client, token, upload_id, last, chunks[last], hashlib.sha256(chunks[last]).hexdigest()
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["data"]["duplicate"] is False

    status = await client.get(f"{BASE}/files/uploads/{upload_id}", headers=auth_header(token))
    assert status.json()["data"]["receivedChunks"] == [last]

    early = await client.post(
        f"{BASE}/files/uploads/{upload_id}/complete", headers=auth_header(token)
    )
    assert early.status_code == 409
    assert early.json()["error"]["code"] == "chunks_missing"
    assert early.json()["error"]["details"]["missing"] == list(range(last))

    for index in range(last):
        assert (await _put_chunk(client, token, upload_id, index, chunks[index])).status_code == 200

    retry = await _put_chunk(client, token, upload_id, 0, chunks[0])  # the client retries a chunk
    assert retry.json()["data"]["duplicate"] is True

    conflict = await _put_chunk(client, token, upload_id, 0, b"\x00" * len(chunks[0]))
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "chunk_conflict"

    complete = await client.post(
        f"{BASE}/files/uploads/{upload_id}/complete", headers=auth_header(token)
    )
    assert complete.status_code == 202, complete.text
    file_id = complete.json()["data"]["fileId"]
    processing = [call for call in dispatched if call[0] == "files.process_upload"]
    assert processing == [("files.process_upload", {"upload_id": upload_id, "file_id": file_id})]

    again = await client.post(
        f"{BASE}/files/uploads/{upload_id}/complete", headers=auth_header(token)
    )
    assert again.status_code == 202
    assert again.json()["data"]["fileId"] == file_id
    processing_after = [call for call in dispatched if call[0] == "files.process_upload"]
    assert len(processing_after) == 1, "idempotent completion must not dispatch processing twice"


async def test_chunk_rules_size_checksum_and_index(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    storage_root,  # type: ignore[no-untyped-def]
) -> None:
    _, token = await _user(client, tenant_id, session_factory, "rules@example.com")
    session = await _initiate(client, token, size=10, chunk_size=4)
    upload_id = session["uploadId"]

    oversized = await _put_chunk(client, token, upload_id, 0, b"12345")  # expected 4 bytes
    assert oversized.status_code == 413
    assert oversized.json()["error"]["code"] == "chunk_too_large"

    short = await _put_chunk(client, token, upload_id, 0, b"123")
    assert short.status_code == 422
    assert short.json()["error"]["code"] == "chunk_size_mismatch"

    wrong_sha = await _put_chunk(client, token, upload_id, 0, b"1234", sha="0" * 64)
    assert wrong_sha.status_code == 422
    assert wrong_sha.json()["error"]["code"] == "checksum_mismatch"

    out_of_range = await _put_chunk(client, token, upload_id, 9, b"1234")
    assert out_of_range.status_code == 422
    assert out_of_range.json()["error"]["code"] == "chunk_index_invalid"

    status = await client.get(f"{BASE}/files/uploads/{upload_id}", headers=auth_header(token))
    assert status.json()["data"]["receivedChunks"] == [], "rejected chunks are never stored"


async def test_initiate_validation_and_session_cap(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    storage_root,  # type: ignore[no-untyped-def]
) -> None:
    _, token = await _user(client, tenant_id, session_factory, "caps@example.com")
    too_big = await client.post(
        f"{BASE}/files/uploads",
        json={"filename": "huge.bin", "size": 2 * 1024**3},
        headers=auth_header(token),
    )
    assert too_big.status_code == 413
    assert too_big.json()["error"]["code"] == "upload_too_large"

    bad_sha = await client.post(
        f"{BASE}/files/uploads",
        json={"filename": "a.bin", "size": 10, "sha256": "not-a-digest"},
        headers=auth_header(token),
    )
    assert bad_sha.status_code == 422

    for _ in range(5):  # default cap is 5 active sessions per user
        await _initiate(client, token, size=10, chunk_size=5)
    sixth = await client.post(
        f"{BASE}/files/uploads", json={"filename": "b.bin", "size": 10}, headers=auth_header(token)
    )
    assert sixth.status_code == 409
    assert sixth.json()["error"]["code"] == "too_many_active_uploads"


async def test_abort_is_idempotent_and_blocks_later_chunks(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    storage_root,  # type: ignore[no-untyped-def]
) -> None:
    _, token = await _user(client, tenant_id, session_factory, "aborter@example.com")
    session = await _initiate(client, token, size=8, chunk_size=4)
    upload_id = session["uploadId"]
    assert (await _put_chunk(client, token, upload_id, 0, b"1234")).status_code == 200

    first = await client.delete(f"{BASE}/files/uploads/{upload_id}", headers=auth_header(token))
    second = await client.delete(f"{BASE}/files/uploads/{upload_id}", headers=auth_header(token))
    assert first.status_code == second.status_code == 204

    after = await _put_chunk(client, token, upload_id, 1, b"5678")
    assert after.status_code == 409
    assert after.json()["error"]["code"] == "upload_state_invalid"


async def test_expired_session_refuses_chunks_with_410(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    storage_root,  # type: ignore[no-untyped-def]
) -> None:
    _, token = await _user(client, tenant_id, session_factory, "expiry@example.com")
    session = await _initiate(client, token, size=8, chunk_size=4)
    async with session_factory() as db:
        await db.execute(
            update(FileUploadModel)
            .where(FileUploadModel.id == UUID(session["uploadId"]))
            .values(expires_at=datetime(2000, 1, 1, tzinfo=UTC))
        )
        await db.commit()

    expired = await _put_chunk(client, token, session["uploadId"], 0, b"1234")
    assert expired.status_code == 410
    assert expired.json()["error"]["code"] == "upload_expired"


async def test_upload_sessions_are_owner_only(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    storage_root,  # type: ignore[no-untyped-def]
) -> None:
    _, owner = await _user(client, tenant_id, session_factory, "owner@example.com")
    _, intruder = await _user(client, tenant_id, session_factory, "intruder@example.com")
    session = await _initiate(client, owner, size=8, chunk_size=4)
    upload_id = session["uploadId"]

    status = await client.get(f"{BASE}/files/uploads/{upload_id}", headers=auth_header(intruder))
    assert status.status_code == 404
    assert (await _put_chunk(client, intruder, upload_id, 0, b"1234")).status_code == 404
    complete = await client.post(
        f"{BASE}/files/uploads/{upload_id}/complete", headers=auth_header(intruder)
    )
    assert complete.status_code == 404
    abort = await client.delete(f"{BASE}/files/uploads/{upload_id}", headers=auth_header(intruder))
    assert abort.status_code == 404


async def test_upload_requires_files_write_permission(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    storage_root,  # type: ignore[no-untyped-def]
) -> None:
    _, token = await _user(
        client, tenant_id, session_factory, "reader@example.com", permissions=["files:read"]
    )
    response = await client.post(
        f"{BASE}/files/uploads", json={"filename": "a.bin", "size": 4}, headers=auth_header(token)
    )
    assert response.status_code == 403


async def test_file_visibility_download_and_delete_rules(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    storage_root,  # type: ignore[no-untyped-def]
) -> None:
    root, _ = storage_root
    _, owner = await _user(client, tenant_id, session_factory, "photo-owner@example.com")
    _, stranger = await _user(
        client, tenant_id, session_factory, "stranger@example.com", permissions=[]
    )
    _, reader = await _user(
        client, tenant_id, session_factory, "reader-all@example.com", permissions=["files:read"]
    )

    data = _png(
        (300, 200)
    )  # large enough that the 128 and 256 variants are produced (no upscaling)
    session = await _initiate(
        client, owner, size=len(data), chunk_size=len(data), visibility="PUBLIC"
    )
    upload_id = session["uploadId"]
    assert (await _put_chunk(client, owner, upload_id, 0, data)).status_code == 200
    done = await client.post(
        f"{BASE}/files/uploads/{upload_id}/complete", headers=auth_header(owner)
    )
    file_id = done.json()["data"]["fileId"]

    # Run the worker pipeline directly, with the same adapters the Celery task uses.
    work = root.parent / "work"
    work.mkdir(parents=True, exist_ok=True)
    settings = get_settings()
    async with session_factory() as db:
        use_case = ProcessUploadUseCase(
            UploadRepository(db),
            ChunkRepository(db),
            FileRepository(db),
            LocalFileSystemStorage(root),
            build_malware_scanner(settings),
            build_image_processor(),
            build_image_options(settings),
            work,
        )
        assert await use_case.execute(UUID(upload_id), UUID(file_id)) == "AVAILABLE"
        await db.commit()

    meta = (await client.get(f"{BASE}/files/{file_id}", headers=auth_header(owner))).json()["data"]
    assert meta["status"] == "AVAILABLE" and meta["kind"] == "IMAGE"
    assert meta["mimeType"] == "image/webp", "the stored master is the re-encoded WebP"
    assert {v["name"] for v in meta["variants"]} == {"original", "128", "256"}
    public_url = next(v["url"] for v in meta["variants"] if v["name"] == "128")
    assert public_url.startswith(f"{BASE}/public/files/{file_id}/")

    content = await client.get(f"{BASE}/files/{file_id}/content", headers=auth_header(owner))
    assert content.status_code == 200
    assert content.headers["content-type"] == "image/webp"
    assert content.headers["content-disposition"].startswith("inline")
    assert content.headers["cache-control"] == "private, no-store"

    public = await client.get(public_url)  # anonymous
    assert public.status_code == 200
    assert public.headers["cache-control"] == "public, max-age=86400"
    assert public.headers["etag"].startswith('"')

    # Anti-IDOR: a stranger without permission sees NotFound, not Forbidden.
    assert (
        await client.get(f"{BASE}/files/{file_id}", headers=auth_header(stranger))
    ).status_code == 404
    assert (
        await client.delete(f"{BASE}/files/{file_id}", headers=auth_header(stranger))
    ).status_code == 404
    # A user with files:read (but not the owner) may read.
    assert (
        await client.get(f"{BASE}/files/{file_id}", headers=auth_header(reader))
    ).status_code == 200

    deleted = await client.delete(f"{BASE}/files/{file_id}", headers=auth_header(owner))
    assert deleted.status_code == 204
    assert (
        await client.get(f"{BASE}/files/{file_id}", headers=auth_header(owner))
    ).status_code == 404
    assert (await client.get(public_url)).status_code == 404, "deleted files stop being served"


async def test_public_route_never_serves_private_or_unknown_files(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    storage_root,  # type: ignore[no-untyped-def]
) -> None:
    unknown = await client.get(f"{BASE}/public/files/9c858901-8a57-4791-81fe-4c455b099bc9/original")
    assert unknown.status_code == 404
    malformed = await client.get(f"{BASE}/public/files/not-a-uuid/original")
    assert malformed.status_code == 422


def test_chunk_endpoint_documents_a_file_body_for_swagger_and_redoc() -> None:
    """Without a requestBody the docs show no input for the chunk bytes, so no file can be
    picked."""
    spec = app.openapi()
    operation = spec["paths"][f"{BASE}/files/uploads/{{upload_id}}/chunks/{{index}}"]["put"]
    body = operation["requestBody"]
    assert body["required"] is True
    assert body["content"]["application/octet-stream"]["schema"] == {
        "type": "string",
        "format": "binary",
    }


async def test_single_request_multipart_upload(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    storage_root,  # type: ignore[no-untyped-def]
) -> None:
    _, dispatched = storage_root
    _, token = await _user(client, tenant_id, session_factory, "multipart@example.com")
    data = _png()
    response = await client.post(
        f"{BASE}/files",
        files={"file": ("holiday.png", data, "image/png")},
        data={"visibility": "PRIVATE"},
        headers=auth_header(token),
    )
    assert response.status_code == 202, response.text
    file_id = response.json()["data"]["fileId"]
    assert response.json()["data"]["status"] == "UPLOADED"
    processing = [call for call in dispatched if call[0] == "files.process_upload"]
    assert processing and processing[0][1]["file_id"] == file_id

    meta = await client.get(f"{BASE}/files/{file_id}", headers=auth_header(token))
    assert meta.status_code == 200
    assert meta.json()["data"]["originalName"] == "holiday.png"


async def test_single_request_upload_rules(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    storage_root,  # type: ignore[no-untyped-def]
) -> None:
    _, token = await _user(client, tenant_id, session_factory, "multipart-rules@example.com")
    too_big = b"x" * (20 * 1024 * 1024 + 1)  # one byte over the single-request limit
    response = await client.post(
        f"{BASE}/files",
        files={"file": ("big.bin", too_big, "application/octet-stream")},
        headers=auth_header(token),
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "single_upload_too_large"

    wrong = await client.post(
        f"{BASE}/files",
        files={"file": ("a.png", _png(), "image/png")},
        data={"sha256": "0" * 64},
        headers=auth_header(token),
    )
    assert wrong.status_code == 422
    assert wrong.json()["error"]["code"] == "checksum_mismatch"


def test_multipart_endpoint_documents_a_file_field_for_swagger() -> None:
    spec = app.openapi()
    body = spec["paths"][f"{BASE}/files"]["post"]["requestBody"]
    schema = body["content"]["multipart/form-data"]["schema"]
    ref = schema.get("$ref", "")
    properties = (
        spec["components"]["schemas"][ref.rsplit("/", 1)[-1]]["properties"]
        if ref
        else schema["properties"]
    )
    assert properties["file"]["format"] == "binary"


async def test_multipart_blank_checksum_field_means_no_checksum(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    storage_root,  # type: ignore[no-untyped-def]
) -> None:
    """Swagger sends the blank field as an empty string; that must not be treated as a checksum."""
    _, token = await _user(client, tenant_id, session_factory, "blank-sha@example.com")
    response = await client.post(
        f"{BASE}/files",
        files={"file": ("a.png", _png(), "image/png")},
        data={"sha256": "", "visibility": "PRIVATE"},
        headers=auth_header(token),
    )
    assert response.status_code == 202, response.text


async def test_oversized_multipart_body_is_refused_while_received(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    storage_root,  # type: ignore[no-untyped-def]
) -> None:
    """The body limit applies before FastAPI spools the multipart body, both with a declared
    Content-Length and with a chunked body that declares none."""
    _, token = await _user(client, tenant_id, session_factory, "body-limit@example.com")
    boundary = "e2eboundary"
    headers = {
        **auth_header(token),
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }
    oversized = 21 * 1024 * 1024

    declared = await client.post(
        f"{BASE}/files",
        content=b"x" * oversized,
        headers={**headers, "Content-Length": str(oversized)},
    )
    assert declared.status_code == 413
    assert declared.json()["error"]["code"] == "payload_too_large"

    async def chunked_body():  # type: ignore[no-untyped-def]
        # A valid multipart body, so the parser would accept it: only the size limit can refuse it.
        yield (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="big.bin"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        for _ in range(oversized // (1024 * 1024)):
            yield b"x" * (1024 * 1024)
        yield f"\r\n--{boundary}--\r\n".encode()

    chunked = await client.post(f"{BASE}/files", content=chunked_body(), headers=headers)
    assert chunked.status_code == 413
    assert chunked.json()["error"]["code"] == "payload_too_large"


async def test_retry_with_the_stored_checksum_is_answered_without_rereading_the_body(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    storage_root,  # type: ignore[no-untyped-def]
) -> None:
    _, token = await _user(client, tenant_id, session_factory, "retry-short@example.com")
    session = await _initiate(client, token, size=8, chunk_size=4)
    upload_id = session["uploadId"]
    first = await _put_chunk(
        client, token, upload_id, 0, b"1234", sha=hashlib.sha256(b"1234").hexdigest()
    )
    assert first.json()["data"]["duplicate"] is False

    retry = await _put_chunk(
        client, token, upload_id, 0, b"zzzz", sha=hashlib.sha256(b"1234").hexdigest()
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["data"]["duplicate"] is True, "the stored chunk is kept, not overwritten"
