"""Worker pipeline against real uploads: hostile content, scanner outcomes, redelivery, cleanup.

The uploads go through the HTTP API (as a client would send them); the pipeline is then run with
the same adapters the Celery task uses, except where a test injects a scanner outcome.
"""

import hashlib
import io
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from httpx import AsyncClient
from PIL import Image
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.celery_app import celery_app
from app.core.config import get_settings
from app.main import app
from app.modules.files.application.pipeline import ProcessUploadUseCase
from app.modules.files.application.use_cases import CleanupFilesUseCase
from app.modules.files.domain.ports import MalwareScannerPort, ScannerUnavailableError, ScanResult
from app.modules.files.domain.value_objects import Region, StorageKey
from app.modules.files.infrastructure.factory import (
    build_image_options,
    build_image_processor,
    build_malware_scanner,
)
from app.modules.files.infrastructure.models import FileModel, FileUploadModel
from app.modules.files.infrastructure.repository import (
    ChunkRepository,
    FileRepository,
    UploadRepository,
)
from app.modules.files.infrastructure.storage.local_fs import LocalFileSystemStorage
from app.modules.files.presentation.router import get_object_storage
from tests.helpers import auth_header, grant_role, register_and_login

BASE = "/api/v1"


class _Scanner(MalwareScannerPort):
    async def self_test(self) -> None:
        return None

    def __init__(self, *, infected: bool = False, unavailable: bool = False) -> None:
        self._infected = infected
        self._unavailable = unavailable

    async def scan(self, path: Path) -> ScanResult:
        if self._unavailable:
            raise ScannerUnavailableError("clamd is down")
        return ScanResult(
            clean=not self._infected, signature="Test.Signature" if self._infected else None
        )


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (300, 200), "navy").save(buffer, "PNG")
    return buffer.getvalue()


async def _one_block(data: bytes) -> AsyncIterator[bytes]:
    yield data


async def _owner(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> str:
    user_id, token, _ = await register_and_login(
        client, tenant_id, f"pipe-{uuid4().hex[:8]}@example.com"
    )
    await grant_role(
        session_factory,
        tenant_id=tenant_id,
        user_id=user_id,
        role_name="files",
        permissions=["files:read", "files:write", "files:delete"],
    )
    return token


async def _upload(
    client: AsyncClient,
    token: str,
    data: bytes,
    *,
    filename: str,
    visibility: str = "PRIVATE",
    sha256: str | None = None,
) -> tuple[str, str]:
    body: dict[str, Any] = {"filename": filename, "size": len(data), "visibility": visibility}
    if sha256 is not None:
        body["sha256"] = sha256
    initiated = await client.post(f"{BASE}/files/uploads", json=body, headers=auth_header(token))
    assert initiated.status_code == 201, initiated.text
    upload_id = initiated.json()["data"]["uploadId"]
    put = await client.put(
        f"{BASE}/files/uploads/{upload_id}/chunks/0",
        content=data,
        headers={**auth_header(token), "Content-Type": "application/octet-stream"},
    )
    assert put.status_code == 200, put.text
    done = await client.post(
        f"{BASE}/files/uploads/{upload_id}/complete", headers=auth_header(token)
    )
    assert done.status_code == 202, done.text
    return upload_id, done.json()["data"]["fileId"]


async def _process(
    session_factory: async_sessionmaker[AsyncSession],
    root: Path,
    upload_id: str,
    file_id: str,
    scanner: MalwareScannerPort | None = None,
) -> str:
    settings = get_settings()
    work = root.parent / "work"
    work.mkdir(parents=True, exist_ok=True)
    async with session_factory() as db:
        status = await ProcessUploadUseCase(
            UploadRepository(db),
            ChunkRepository(db),
            FileRepository(db),
            LocalFileSystemStorage(root),
            scanner or build_malware_scanner(settings),
            build_image_processor(),
            build_image_options(settings),
            work,
        ).execute(UUID(upload_id), UUID(file_id))
        await db.commit()
        return status


async def _status(client: AsyncClient, token: str, file_id: str) -> dict[str, Any]:
    response = await client.get(f"{BASE}/files/{file_id}", headers=auth_header(token))
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _wire_storage(root: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    app.dependency_overrides[get_object_storage] = lambda: LocalFileSystemStorage(root)
    monkeypatch.setattr(celery_app, "send_task", lambda *a, **k: None)


async def test_pdf_is_stored_as_a_download_without_image_processing(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    root = tmp_path / "data"
    _wire_storage(root, monkeypatch)
    token = await _owner(client, tenant_id, session_factory)
    pdf = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n"
    upload_id, file_id = await _upload(client, token, pdf, filename="report.pdf")

    assert await _process(session_factory, root, upload_id, file_id) == "AVAILABLE"
    meta = await _status(client, token, file_id)
    assert meta["kind"] == "DOCUMENT" and meta["mimeType"] == "application/pdf"
    assert [v["name"] for v in meta["variants"]] == ["original"]
    content = await client.get(f"{BASE}/files/{file_id}/content", headers=auth_header(token))
    assert content.content == pdf
    assert content.headers["content-disposition"].startswith("attachment")


async def test_executable_disguised_as_an_image_is_rejected(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    root = tmp_path / "data"
    _wire_storage(root, monkeypatch)
    token = await _owner(client, tenant_id, session_factory)
    # Filename and client intent say "photo.png"; the bytes are a Windows executable.
    upload_id, file_id = await _upload(
        client, token, b"MZ\x90\x00" + b"\x00" * 200, filename="photo.png"
    )

    assert await _process(session_factory, root, upload_id, file_id) == "REJECTED"
    meta = await _status(client, token, file_id)
    assert meta["status"] == "REJECTED" and meta["failureReason"] == "DISALLOWED_TYPE"
    assert not (root / "private" / "files" / file_id).exists(), "rejected artifacts are removed"


async def test_html_markup_cannot_pass_as_a_generic_file(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    root = tmp_path / "data"
    _wire_storage(root, monkeypatch)
    token = await _owner(client, tenant_id, session_factory)
    payload = b"<!DOCTYPE html><html><script>alert(document.cookie)</script></html>"
    upload_id, file_id = await _upload(client, token, payload, filename="notes.txt")

    assert await _process(session_factory, root, upload_id, file_id) == "REJECTED"
    assert (await _status(client, token, file_id))["failureReason"] == "DISALLOWED_TYPE"


async def test_infected_upload_is_rejected_when_the_scanner_flags_it(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    root = tmp_path / "data"
    _wire_storage(root, monkeypatch)
    token = await _owner(client, tenant_id, session_factory)
    upload_id, file_id = await _upload(client, token, _png(), filename="scan.png")

    status = await _process(
        session_factory, root, upload_id, file_id, scanner=_Scanner(infected=True)
    )
    assert status == "REJECTED"
    assert (await _status(client, token, file_id))["failureReason"] == "MALWARE"


async def test_unavailable_scanner_fails_closed(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    root = tmp_path / "data"
    _wire_storage(root, monkeypatch)
    token = await _owner(client, tenant_id, session_factory)
    upload_id, file_id = await _upload(client, token, _png(), filename="scan.png")

    status = await _process(
        session_factory, root, upload_id, file_id, scanner=_Scanner(unavailable=True)
    )
    assert status == "FAILED"  # never AVAILABLE without a verdict
    assert (await _status(client, token, file_id))["failureReason"] == "SCAN_UNAVAILABLE"


async def test_declared_checksum_mismatch_fails_the_file(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    root = tmp_path / "data"
    _wire_storage(root, monkeypatch)
    token = await _owner(client, tenant_id, session_factory)
    wrong = hashlib.sha256(b"something else").hexdigest()
    upload_id, file_id = await _upload(client, token, _png(), filename="a.png", sha256=wrong)

    assert await _process(session_factory, root, upload_id, file_id) == "FAILED"
    assert (await _status(client, token, file_id))["failureReason"] == "CHECKSUM_MISMATCH"


async def test_redelivered_task_does_not_repeat_or_break_a_finished_file(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    root = tmp_path / "data"
    _wire_storage(root, monkeypatch)
    token = await _owner(client, tenant_id, session_factory)
    upload_id, file_id = await _upload(client, token, _png(), filename="a.png", visibility="PUBLIC")

    assert await _process(session_factory, root, upload_id, file_id) == "AVAILABLE"
    assert await _process(session_factory, root, upload_id, file_id) == "AVAILABLE"
    assert (await _status(client, token, file_id))["status"] == "AVAILABLE"


async def test_cleanup_expires_stale_sessions_and_is_idempotent(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    root = tmp_path / "data"
    storage = LocalFileSystemStorage(root)
    _wire_storage(root, monkeypatch)
    token = await _owner(client, tenant_id, session_factory)

    initiated = await client.post(
        f"{BASE}/files/uploads",
        json={"filename": "old.bin", "size": 8, "chunkSize": 4},
        headers=auth_header(token),
    )
    upload_id = initiated.json()["data"]["uploadId"]
    await client.put(
        f"{BASE}/files/uploads/{upload_id}/chunks/0",
        content=b"1234",
        headers={**auth_header(token), "Content-Type": "application/octet-stream"},
    )
    async with session_factory() as db:
        await db.execute(
            update(FileUploadModel)
            .where(FileUploadModel.id == UUID(upload_id))
            .values(expires_at=datetime(2000, 1, 1, tzinfo=UTC))
        )
        await db.commit()
    # A directory that no session owns, e.g. left behind by a crash.
    orphan = StorageKey(Region.TMP, ("uploads", str(uuid4()), "chunks", "000000.part"))
    await storage.write(orphan, _one_block(b"junk"))

    async def run() -> int:
        async with session_factory() as db:
            report = await CleanupFilesUseCase(
                UploadRepository(db), FileRepository(db), storage, stale_after=timedelta(0)
            ).execute()
            await db.commit()
            return report.expired_sessions

    # The database is shared by the whole suite, so other tests' stale sessions may be expired too.
    # Assert on this test's own session instead of an exact global count.
    assert await run() >= 1
    async with session_factory() as db:
        status = (
            await db.execute(
                select(FileUploadModel.status).where(FileUploadModel.id == UUID(upload_id))
            )
        ).scalar_one()
    assert status == "EXPIRED"
    assert not (root / "tmp" / "uploads" / upload_id).exists()
    assert not await storage.exists(orphan)
    assert await run() == 0  # idempotent: nothing left to change


async def test_finished_artifacts_are_purged_once_and_marked(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    root = tmp_path / "data"
    storage = LocalFileSystemStorage(root)
    _wire_storage(root, monkeypatch)
    token = await _owner(client, tenant_id, session_factory)
    upload_id, file_id = await _upload(
        client, token, b"MZ\x90\x00" + b"\x00" * 200, filename="photo.png"
    )
    assert await _process(session_factory, root, upload_id, file_id) == "REJECTED"

    async def run() -> int:
        async with session_factory() as db:
            report = await CleanupFilesUseCase(
                UploadRepository(db), FileRepository(db), storage, stale_after=timedelta(0)
            ).execute()
            await db.commit()
            return report.purged_file_trees

    assert await run() >= 1
    async with session_factory() as db:
        marked = (
            await db.execute(
                select(FileModel.storage_purged_at).where(FileModel.id == UUID(file_id))
            )
        ).scalar_one()
    assert marked is not None, "the purge is recorded so later runs skip the file"
    assert not (root / "private" / "files" / file_id).exists()
    assert await run() == 0


async def test_fresh_upload_directories_are_not_removed_by_cleanup(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A directory touched recently may belong to a COMPLETED upload the worker is still reading."""
    storage = LocalFileSystemStorage(tmp_path / "data")
    fresh = StorageKey(Region.TMP, ("uploads", str(uuid4()), "chunks", "000000.part"))
    await storage.write(fresh, _one_block(b"chunk still needed"))
    async with session_factory() as db:
        await CleanupFilesUseCase(UploadRepository(db), FileRepository(db), storage).execute()
        await db.commit()
    assert await storage.exists(fresh), "a recent directory is kept until it goes stale"


async def test_non_uuid_upload_directory_names_are_ignored_by_cleanup(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """`uploads/` is only ever written by this app with a UUID directory name, but the sweep
    still guards against anything else landing there (e.g. a stray file from manual poking at
    the storage root) instead of crashing or deleting something it shouldn't."""
    storage = LocalFileSystemStorage(tmp_path / "data")
    not_a_uuid = StorageKey(Region.TMP, ("uploads", "not-a-uuid", "chunks", "000000.part"))
    await storage.write(not_a_uuid, _one_block(b"junk"))
    async with session_factory() as db:
        report = await CleanupFilesUseCase(
            UploadRepository(db), FileRepository(db), storage, stale_after=timedelta(0)
        ).execute()
        await db.commit()
    assert report.purged_upload_dirs == 0
    assert await storage.exists(not_a_uuid)
