"""Pure use-case tests for the avatar feature: no DB, no HTTP, no real files-module adapters."""

from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import uuid4

import pytest

from app.modules.files.application.use_cases import UploadPolicy
from app.modules.files.domain.entities import FileAsset
from app.modules.files.domain.state import FileStatus
from app.modules.files.domain.value_objects import FileKind, Visibility
from app.modules.users.application.dto import UpdateAvatarInput
from app.modules.users.application.use_cases import RemoveAvatarUseCase, UpdateAvatarUseCase
from app.modules.users.domain.entities import User
from app.modules.users.domain.exceptions import (
    AvatarChangeInProgressError,
    UserHasNoAvatarError,
    UserNotFoundError,
)
from tests.fakes import (
    InMemoryChunkRepository,
    InMemoryFileRepository,
    InMemoryObjectStorage,
    InMemoryUploadRepository,
    InMemoryUserRepository,
)

_POLICY = UploadPolicy(
    chunk_max_bytes=10 * 1024 * 1024,
    max_file_bytes=10 * 1024 * 1024,
    session_ttl=timedelta(hours=1),
    max_active_sessions=5,
)


async def _one_block(data: bytes) -> AsyncIterator[bytes]:
    yield data


def _upload_input(
    user_id: object, tenant_id: object, filename: str = "a.png", caller_id: object = None
) -> UpdateAvatarInput:
    return UpdateAvatarInput(
        target_user_id=user_id,  # type: ignore[arg-type]
        caller_id=caller_id if caller_id is not None else user_id,  # type: ignore[arg-type]
        tenant_id=tenant_id,  # type: ignore[arg-type]
        filename=filename,
        size=4,
        sha256=None,
        body=_one_block(b"data"),
    )


async def test_update_avatar_first_upload_sets_avatar_file_id() -> None:
    users = InMemoryUserRepository()
    user = User(tenant_id=uuid4(), email="a@example.com", hashed_password="h", full_name="A")
    await users.add(user)
    files_repo = InMemoryFileRepository()
    use_case = UpdateAvatarUseCase(
        users,
        InMemoryUploadRepository(),
        InMemoryChunkRepository(),
        files_repo,
        InMemoryObjectStorage(),
        _POLICY,
    )

    updated, _upload_id, file_id = await use_case.execute(_upload_input(user.id, user.tenant_id))

    assert updated.avatar_file_id == file_id
    assert users.users[user.id].avatar_file_id == file_id
    assert file_id in files_repo.files


async def test_update_avatar_user_not_found_raises() -> None:
    use_case = UpdateAvatarUseCase(
        InMemoryUserRepository(),
        InMemoryUploadRepository(),
        InMemoryChunkRepository(),
        InMemoryFileRepository(),
        InMemoryObjectStorage(),
        _POLICY,
    )
    with pytest.raises(UserNotFoundError):
        await use_case.execute(_upload_input(uuid4(), uuid4()))


async def test_update_avatar_replaces_available_previous_avatar() -> None:
    users = InMemoryUserRepository()
    tenant_id = uuid4()
    user = User(tenant_id=tenant_id, email="a@example.com", hashed_password="h", full_name="A")
    await users.add(user)
    files_repo = InMemoryFileRepository()
    uploads = InMemoryUploadRepository()
    use_case = UpdateAvatarUseCase(
        users, uploads, InMemoryChunkRepository(), files_repo, InMemoryObjectStorage(), _POLICY
    )

    _, _, first_file_id = await use_case.execute(_upload_input(user.id, tenant_id, "a.png"))
    # Simulate the files pipeline finishing: the first avatar becomes AVAILABLE.
    files_repo.files[first_file_id].status = FileStatus.AVAILABLE

    admin_id = uuid4()
    _, _, second_file_id = await use_case.execute(
        _upload_input(user.id, tenant_id, "b.png", caller_id=admin_id)
    )

    assert files_repo.files[first_file_id].status is FileStatus.DELETED
    assert files_repo.files[first_file_id].deleted_by == admin_id  # the acting admin, not the owner
    assert users.users[user.id].avatar_file_id == second_file_id


async def test_update_avatar_raises_while_previous_still_processing() -> None:
    users = InMemoryUserRepository()
    tenant_id = uuid4()
    user = User(tenant_id=tenant_id, email="a@example.com", hashed_password="h", full_name="A")
    await users.add(user)
    files_repo = InMemoryFileRepository()
    uploads = InMemoryUploadRepository()
    use_case = UpdateAvatarUseCase(
        users, uploads, InMemoryChunkRepository(), files_repo, InMemoryObjectStorage(), _POLICY
    )

    _, _, first_file_id = await use_case.execute(_upload_input(user.id, tenant_id, "a.png"))
    assert files_repo.files[first_file_id].status is FileStatus.UPLOADED  # left mid-pipeline

    with pytest.raises(AvatarChangeInProgressError):
        await use_case.execute(_upload_input(user.id, tenant_id, "b.png"))

    assert len(uploads.sessions) == 1  # no second upload session was ever started


async def test_update_avatar_proceeds_when_previous_is_terminal_bad() -> None:
    users = InMemoryUserRepository()
    tenant_id = uuid4()
    user = User(tenant_id=tenant_id, email="a@example.com", hashed_password="h", full_name="A")
    await users.add(user)
    files_repo = InMemoryFileRepository()
    uploads = InMemoryUploadRepository()
    use_case = UpdateAvatarUseCase(
        users, uploads, InMemoryChunkRepository(), files_repo, InMemoryObjectStorage(), _POLICY
    )

    _, _, first_file_id = await use_case.execute(_upload_input(user.id, tenant_id, "a.png"))
    files_repo.files[first_file_id].status = FileStatus.FAILED

    _, _, second_file_id = await use_case.execute(_upload_input(user.id, tenant_id, "b.png"))

    assert users.users[user.id].avatar_file_id == second_file_id
    # Untouched: nothing to delete for an already-FAILED file.
    assert files_repo.files[first_file_id].status is FileStatus.FAILED


async def test_remove_avatar_unknown_user_raises_not_found() -> None:
    with pytest.raises(UserNotFoundError):
        await RemoveAvatarUseCase(InMemoryUserRepository(), InMemoryFileRepository()).execute(
            uuid4(), uuid4()
        )


async def test_remove_avatar_no_avatar_raises() -> None:
    users = InMemoryUserRepository()
    user = User(tenant_id=uuid4(), email="a@example.com", hashed_password="h", full_name="A")
    await users.add(user)
    with pytest.raises(UserHasNoAvatarError):
        await RemoveAvatarUseCase(users, InMemoryFileRepository()).execute(user.id, user.id)


async def test_remove_avatar_available_clears_field_and_deletes_file() -> None:
    users = InMemoryUserRepository()
    tenant_id = uuid4()
    file_id = uuid4()
    user = User(
        tenant_id=tenant_id,
        email="a@example.com",
        hashed_password="h",
        full_name="A",
        avatar_file_id=file_id,
    )
    await users.add(user)
    files_repo = InMemoryFileRepository()
    files_repo.files[file_id] = FileAsset(
        id=file_id,
        tenant_id=tenant_id,
        owner_id=user.id,
        status=FileStatus.AVAILABLE,
        visibility=Visibility.PUBLIC,
        kind=FileKind.IMAGE,
    )

    admin_id = uuid4()
    updated = await RemoveAvatarUseCase(users, files_repo).execute(user.id, admin_id)

    assert updated.avatar_file_id is None
    assert files_repo.files[file_id].status is FileStatus.DELETED
    assert files_repo.files[file_id].deleted_by == admin_id  # the acting admin, not the owner


async def test_remove_avatar_still_processing_clears_field_but_skips_delete() -> None:
    users = InMemoryUserRepository()
    tenant_id = uuid4()
    file_id = uuid4()
    user = User(
        tenant_id=tenant_id,
        email="a@example.com",
        hashed_password="h",
        full_name="A",
        avatar_file_id=file_id,
    )
    await users.add(user)
    files_repo = InMemoryFileRepository()
    files_repo.files[file_id] = FileAsset(
        id=file_id,
        tenant_id=tenant_id,
        owner_id=user.id,
        status=FileStatus.PROCESSING,
        visibility=Visibility.PUBLIC,
        kind=FileKind.IMAGE,
    )

    updated = await RemoveAvatarUseCase(users, files_repo).execute(user.id, user.id)

    assert updated.avatar_file_id is None
    assert files_repo.files[file_id].status is FileStatus.PROCESSING  # untouched
