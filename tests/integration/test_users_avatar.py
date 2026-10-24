"""End-to-end HTTP tests for the user-avatar feature (SQLite test DB, Redis for auth, temp local
storage). The files module's own processing pipeline is driven directly (as in
`test_files_pipeline.py`) instead of via a real Celery worker; `users.finalize_avatar` is invoked
through `asyncio.to_thread` because the task runs its own `asyncio.run()` internally (see
`test_files_tasks.py`), which cannot nest inside the already-running pytest-asyncio loop.
"""

import asyncio
import io
from pathlib import Path
from typing import Any
from uuid import UUID

from httpx import AsyncClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.celery_app import celery_app
from app.core.config import get_settings
from app.core.di import get_object_storage
from app.main import app
from app.modules.files.application.pipeline import ProcessUploadUseCase
from app.modules.files.domain.state import FileStatus
from app.modules.files.domain.value_objects import FileKind
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
from app.modules.files.presentation.router import (
    get_object_storage as get_files_object_storage,
)
from app.modules.users.infrastructure import tasks as user_tasks
from app.modules.users.infrastructure.repository import UserRepository
from tests.helpers import audit_entries, auth_header, grant_role, register_and_login

BASE = "/api/v1"


def _png(color: str = "teal") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (40, 30), color).save(buffer, "PNG")
    return buffer.getvalue()


def _storage_root(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    # Two separate DI providers return an ObjectStoragePort: the users module's own (used by the
    # avatar upload routes, added in core/di.py) and the files module's own (used by its public
    # serving route, `GET /public/files/{id}/{variant}`) — both must point at the same temp root.
    app.dependency_overrides[get_object_storage] = lambda: LocalFileSystemStorage(root)
    app.dependency_overrides[get_files_object_storage] = lambda: LocalFileSystemStorage(root)
    return root


def _capture_dispatched(monkeypatch: Any) -> list[tuple[str, dict[str, Any]]]:
    dispatched: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        celery_app,
        "send_task",
        lambda name, kwargs=None, **_: dispatched.append((name, kwargs or {})),
    )
    return dispatched


async def _admin(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    email: str,
) -> tuple[str, str]:
    """Returns (user_id, access_token) for a user holding `users:write`."""
    user_id, token, _ = await register_and_login(client, tenant_id, email)
    await grant_role(
        session_factory,
        tenant_id=tenant_id,
        user_id=user_id,
        role_name=f"admin-{email}",
        permissions=["users:write", "audit_log:read"],
    )
    return user_id, token


async def _upload_avatar(
    client: AsyncClient,
    token: str,
    *,
    target_user_id: str | None = None,
    data: bytes,
    content_type: str = "image/png",
    filename: str = "avatar.png",
) -> Any:
    path = f"{BASE}/users/{target_user_id}/avatar" if target_user_id else f"{BASE}/users/me/avatar"
    return await client.put(
        path, files={"file": (filename, data, content_type)}, headers=auth_header(token)
    )


async def _upload_id_for_file(
    session_factory: async_sessionmaker[AsyncSession], file_id: str
) -> str:
    async with session_factory() as db:
        stmt = select(FileUploadModel).where(FileUploadModel.file_id == UUID(file_id))
        row = (await db.execute(stmt)).scalar_one()
        return str(row.id)


async def _process(
    session_factory: async_sessionmaker[AsyncSession], root: Path, upload_id: str, file_id: str
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
            build_malware_scanner(settings),
            build_image_processor(),
            build_image_options(settings),
            work,
        ).execute(UUID(upload_id), UUID(file_id))
        await db.commit()
        return status


async def _upload_and_process(
    client: AsyncClient,
    token: str,
    session_factory: async_sessionmaker[AsyncSession],
    root: Path,
    *,
    target_user_id: str | None = None,
    color: str = "teal",
) -> str:
    """Uploads an avatar and runs the pipeline synchronously to AVAILABLE. Returns the file id."""
    response = await _upload_avatar(client, token, target_user_id=target_user_id, data=_png(color))
    assert response.status_code == 202, response.text
    file_id: str = response.json()["data"]["avatarFileId"]
    upload_id = await _upload_id_for_file(session_factory, file_id)
    status = await _process(session_factory, root, upload_id, file_id)
    assert status == "AVAILABLE", status
    return file_id


async def _finalize(user_id: str, file_id: str) -> str:
    # The task owns its own event loop (`asyncio.run` inside `finalize_avatar_task`), so it must
    # run off-thread here rather than being awaited directly inside this already-running loop.
    return await asyncio.to_thread(
        user_tasks.finalize_avatar_task, user_id=user_id, file_id=file_id
    )


async def _file_status(
    session_factory: async_sessionmaker[AsyncSession], file_id: str
) -> tuple[FileStatus, FileKind]:
    async with session_factory() as db:
        # include_deleted=True: a DELETED file is soft-deleted, so the port's own
        # get_file_unscoped (which never includes deleted rows) can no longer see it once this
        # test is checking the *outcome* of a delete.
        asset = await FileRepository(db).get_by_id(
            UUID(file_id), tenant_scoped=False, include_deleted=True
        )
        assert asset is not None
        return asset.status, asset.kind


async def _avatar_file_id(
    session_factory: async_sessionmaker[AsyncSession], user_id: str
) -> str | None:
    async with session_factory() as db:
        user = await UserRepository(db).get_by_id(UUID(user_id), tenant_scoped=False)
        assert user is not None
        return str(user.avatar_file_id) if user.avatar_file_id else None


async def test_self_service_upload_becomes_available_and_servable(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    root = _storage_root(tmp_path)
    _capture_dispatched(monkeypatch)
    _, token, _ = await register_and_login(client, tenant_id, "self-avatar@example.com")

    response = await _upload_avatar(client, token, data=_png())
    assert response.status_code == 202, response.text
    file_id = response.json()["data"]["avatarFileId"]
    assert file_id is not None

    me_before = await client.get(f"{BASE}/auth/me", headers=auth_header(token))
    assert me_before.json()["data"]["avatarFileId"] == file_id
    avatar_url = me_before.json()["data"]["avatarUrl"]
    # avatarUrl is derived from avatarFileId alone (no extra per-request FileStatus lookup),
    # so it is populated immediately but 404s until processing finishes.
    assert avatar_url is not None
    not_ready_yet = await client.get(avatar_url)
    assert not_ready_yet.status_code == 404

    upload_id = await _upload_id_for_file(session_factory, file_id)
    status = await _process(session_factory, root, upload_id, file_id)
    assert status == "AVAILABLE"

    served = await client.get(avatar_url)
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/webp"


async def test_self_service_replace_deletes_previous_avatar(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    root = _storage_root(tmp_path)
    _capture_dispatched(monkeypatch)
    user_id, token, _ = await register_and_login(client, tenant_id, "replace@example.com")

    first_file_id = await _upload_and_process(client, token, session_factory, root, color="red")
    second_file_id = await _upload_and_process(client, token, session_factory, root, color="blue")

    assert second_file_id != first_file_id
    status, _kind = await _file_status(session_factory, first_file_id)
    assert status is FileStatus.DELETED
    assert await _avatar_file_id(session_factory, user_id) == second_file_id


async def test_self_service_replace_while_processing_returns_409(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    _storage_root(tmp_path)
    _capture_dispatched(monkeypatch)
    _, token, _ = await register_and_login(client, tenant_id, "inflight@example.com")

    first = await _upload_avatar(client, token, data=_png())
    assert first.status_code == 202, first.text  # left UPLOADED on purpose: pipeline not run

    second = await _upload_avatar(client, token, data=_png("blue"))
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "avatar_change_in_progress"


async def test_self_service_remove_clears_avatar_then_404_on_repeat(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    root = _storage_root(tmp_path)
    _capture_dispatched(monkeypatch)
    _, token, _ = await register_and_login(client, tenant_id, "remover@example.com")
    await _upload_and_process(client, token, session_factory, root)

    removed = await client.delete(f"{BASE}/users/me/avatar", headers=auth_header(token))
    assert removed.status_code == 204, removed.text

    me = await client.get(f"{BASE}/auth/me", headers=auth_header(token))
    assert me.json()["data"]["avatarFileId"] is None
    assert me.json()["data"]["avatarUrl"] is None

    again = await client.delete(f"{BASE}/users/me/avatar", headers=auth_header(token))
    assert again.status_code == 404
    assert again.json()["error"]["code"] == "user_has_no_avatar"


async def test_admin_can_manage_other_users_avatar(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    root = _storage_root(tmp_path)
    _capture_dispatched(monkeypatch)
    _, admin_token = await _admin(client, tenant_id, session_factory, "admin-avatar@example.com")
    victim_id, _, _ = await register_and_login(client, tenant_id, "victim-avatar@example.com")

    file_id = await _upload_and_process(
        client, admin_token, session_factory, root, target_user_id=victim_id
    )
    assert await _avatar_file_id(session_factory, victim_id) == file_id

    removed = await client.delete(
        f"{BASE}/users/{victim_id}/avatar", headers=auth_header(admin_token)
    )
    assert removed.status_code == 204, removed.text
    assert await _avatar_file_id(session_factory, victim_id) is None


async def test_non_admin_forbidden_for_other_users_avatar(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, token, _ = await register_and_login(client, tenant_id, "plain-user@example.com")
    victim_id, _, _ = await register_and_login(client, tenant_id, "victim-plain@example.com")

    response = await _upload_avatar(client, token, target_user_id=victim_id, data=_png())
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "insufficient_permission"

    delete_response = await client.delete(
        f"{BASE}/users/{victim_id}/avatar", headers=auth_header(token)
    )
    assert delete_response.status_code == 403


async def test_non_image_content_type_rejected_at_accept_time(
    client: AsyncClient, tenant_id: str
) -> None:
    _, token, _ = await register_and_login(client, tenant_id, "wrong-type@example.com")

    response = await _upload_avatar(
        client, token, data=b"not an image", content_type="text/plain", filename="notes.txt"
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "avatar_must_be_image"


async def test_avatar_larger_than_the_configured_max_is_rejected(
    client: AsyncClient, tenant_id: str
) -> None:
    _, token, _ = await register_and_login(client, tenant_id, "too-big@example.com")
    oversized = b"\x00" * (get_settings().avatar_max_bytes + 1)

    response = await _upload_avatar(client, token, data=oversized, filename="huge.png")

    assert response.status_code == 413, response.text
    assert response.json()["error"]["code"] == "avatar_too_large"


async def test_finalize_task_detaches_non_image_file_that_slipped_through(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    root = _storage_root(tmp_path)
    _capture_dispatched(monkeypatch)
    user_id, token, _ = await register_and_login(client, tenant_id, "spoofed@example.com")

    # Spoofed Content-Type bypasses the cheap accept-time check; the files pipeline's own
    # content-signature detection (not the client-declared type) decides `kind` for real.
    response = await _upload_avatar(
        client,
        token,
        data=b"plain text content, not an image",
        content_type="image/png",
        filename="fake.png",
    )
    assert response.status_code == 202, response.text
    file_id = response.json()["data"]["avatarFileId"]
    upload_id = await _upload_id_for_file(session_factory, file_id)

    processed_status = await _process(session_factory, root, upload_id, file_id)
    assert processed_status == "AVAILABLE"
    status, kind = await _file_status(session_factory, file_id)
    assert (status, kind) == (FileStatus.AVAILABLE, FileKind.GENERIC)

    outcome = await _finalize(user_id, file_id)
    assert outcome.startswith("rejected:")

    assert await _avatar_file_id(session_factory, user_id) is None
    status_after, _ = await _file_status(session_factory, file_id)
    assert status_after is FileStatus.DELETED


async def test_avatar_audit_events_use_target_user_as_resource_id(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    root = _storage_root(tmp_path)
    _capture_dispatched(monkeypatch)
    _, admin_token = await _admin(client, tenant_id, session_factory, "admin-audit@example.com")
    victim_id, _, _ = await register_and_login(client, tenant_id, "victim-audit@example.com")

    await _upload_and_process(client, admin_token, session_factory, root, target_user_id=victim_id)
    (updated_entry,) = await audit_entries(client, admin_token, "user.avatar_updated")
    assert updated_entry["resourceId"] == victim_id

    await client.delete(f"{BASE}/users/{victim_id}/avatar", headers=auth_header(admin_token))
    (removed_entry,) = await audit_entries(client, admin_token, "user.avatar_removed")
    assert removed_entry["resourceId"] == victim_id
