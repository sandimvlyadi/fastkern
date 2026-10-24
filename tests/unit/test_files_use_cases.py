"""Validation and not-found/not-available branches of the files module's use cases, exercised
with the in-memory fakes from `tests/fakes` — the happy paths are already covered end-to-end by
`tests/integration/test_files_api.py`."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.modules.files.application.use_cases import (
    AbortUploadUseCase,
    CompleteUploadUseCase,
    DeleteFileUseCase,
    GetFileUseCase,
    InitiateUploadInput,
    InitiateUploadUseCase,
    UploadChunkUseCase,
    UploadPolicy,
)
from app.modules.files.domain.entities import FileAsset, UploadChunk, UploadSession
from app.modules.files.domain.exceptions import (
    ChecksumMismatchError,
    ChunkTooLargeError,
    FileAssetNotFoundError,
    FileNotAvailableError,
    InvalidUploadError,
    UploadStateError,
)
from app.modules.files.domain.state import FileStatus, UploadStatus
from app.modules.files.domain.value_objects import StorageKey, Visibility
from tests.fakes import (
    InMemoryChunkRepository,
    InMemoryFileRepository,
    InMemoryObjectStorage,
    InMemoryUploadRepository,
)

_POLICY = UploadPolicy(
    chunk_max_bytes=1024,
    max_file_bytes=10_000,
    session_ttl=timedelta(hours=1),
    max_active_sessions=5,
)


def _initiate_use_case() -> InitiateUploadUseCase:
    return InitiateUploadUseCase(InMemoryUploadRepository(), _POLICY)


async def test_initiate_upload_rejects_a_non_positive_size() -> None:
    with pytest.raises(InvalidUploadError):
        await _initiate_use_case().execute(
            InitiateUploadInput(
                filename="f.txt",
                size=0,
                chunk_size=None,
                sha256=None,
                visibility=Visibility.PRIVATE,
            ),
            tenant_id=uuid4(),
            owner_id=uuid4(),
        )


async def test_initiate_upload_rejects_an_out_of_range_chunk_size() -> None:
    with pytest.raises(InvalidUploadError):
        await _initiate_use_case().execute(
            InitiateUploadInput(
                filename="f.txt",
                size=100,
                chunk_size=_POLICY.chunk_max_bytes + 1,
                sha256=None,
                visibility=Visibility.PRIVATE,
            ),
            tenant_id=uuid4(),
            owner_id=uuid4(),
        )


async def test_initiate_upload_rejects_a_malformed_sha256() -> None:
    with pytest.raises(InvalidUploadError):
        await _initiate_use_case().execute(
            InitiateUploadInput(
                filename="f.txt",
                size=100,
                chunk_size=None,
                sha256="not-a-valid-hex-digest",
                visibility=Visibility.PRIVATE,
            ),
            tenant_id=uuid4(),
            owner_id=uuid4(),
        )


def _upload_session(*, chunk_size: int = 1024, declared_size: int = 10) -> UploadSession:
    return UploadSession(
        tenant_id=uuid4(),
        owner_id=uuid4(),
        filename="f.txt",
        declared_size=declared_size,
        chunk_size=chunk_size,
        total_chunks=1,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


async def _empty_body() -> AsyncIterator[bytes]:
    return
    yield b""  # pragma: no cover - never reached, satisfies the generator protocol


async def test_upload_chunk_rejects_a_malformed_declared_checksum() -> None:
    uploads = InMemoryUploadRepository()
    session = _upload_session()
    await uploads.add_session(session)
    use_case = UploadChunkUseCase(uploads, InMemoryChunkRepository(), InMemoryObjectStorage())

    with pytest.raises(ChecksumMismatchError):
        await use_case.execute(
            upload_id=session.id,
            owner_id=session.owner_id,
            index=0,
            body=_empty_body(),
            declared_sha256="not-a-valid-hex-digest",
            content_length=None,
        )


async def test_upload_chunk_rejects_a_body_larger_than_the_expected_chunk_size() -> None:
    """`content_length` is only a fast-reject hint; the real guard is the streamed byte count
    (`_bounded`), so omitting it is what reaches that check."""
    uploads = InMemoryUploadRepository()
    session = _upload_session(declared_size=4, chunk_size=4)
    await uploads.add_session(session)
    use_case = UploadChunkUseCase(uploads, InMemoryChunkRepository(), InMemoryObjectStorage())

    async def _oversized_body() -> AsyncIterator[bytes]:
        yield b"way too many bytes for this chunk"

    with pytest.raises(ChunkTooLargeError):
        await use_case.execute(
            upload_id=session.id,
            owner_id=session.owner_id,
            index=0,
            body=_oversized_body(),
            declared_sha256=None,
            content_length=None,
        )


async def test_get_file_unknown_variant_name_is_not_found() -> None:
    files = InMemoryFileRepository()
    owner_id = uuid4()
    asset = FileAsset(tenant_id=uuid4(), owner_id=owner_id, status=FileStatus.AVAILABLE)
    await files.add_file(asset)

    with pytest.raises(FileAssetNotFoundError):
        await GetFileUseCase(files).resolve_variant(
            asset.id, owner_id, can_read_all=False, variant_name="does-not-exist"
        )


async def test_get_file_resolve_variant_rejects_a_not_yet_available_file() -> None:
    files = InMemoryFileRepository()
    owner_id = uuid4()
    asset = FileAsset(tenant_id=uuid4(), owner_id=owner_id, status=FileStatus.PROCESSING)
    await files.add_file(asset)

    with pytest.raises(FileNotAvailableError):
        await GetFileUseCase(files).resolve_variant(
            asset.id, owner_id, can_read_all=False, variant_name="original"
        )


async def test_get_file_visibility_is_irrelevant_to_access() -> None:
    """Sanity check that a PUBLIC file is still reachable through the owner-gated use case —
    `ResolvePublicVariantUseCase` is the anonymous path, this one always requires ownership
    or `can_read_all`."""
    files = InMemoryFileRepository()
    owner_id = uuid4()
    asset = FileAsset(
        tenant_id=uuid4(),
        owner_id=owner_id,
        status=FileStatus.AVAILABLE,
        visibility=Visibility.PUBLIC,
    )
    await files.add_file(asset)

    fetched, _variants = await GetFileUseCase(files).execute(asset.id, owner_id, can_read_all=False)
    assert fetched.id == asset.id


class _NeverTransitionsUploadRepository(InMemoryUploadRepository):
    """Simulates losing a race to a concurrent complete/abort: the expected-status check in the
    real repository would fail because something else already moved the row, which here is
    forced unconditionally rather than relying on real concurrency."""

    async def transition_session(
        self, upload_id: object, expected: object, new: object, **fields: object
    ) -> bool:
        return False


class _NeverTransitionsFileRepository(InMemoryFileRepository):
    async def transition_file(
        self, file_id: object, expected: object, new: object, **fields: object
    ) -> bool:
        return False


class _MoveFailsObjectStorage(InMemoryObjectStorage):
    async def move(self, source: StorageKey, destination: StorageKey) -> None:
        raise OSError("simulated move failure")


async def test_complete_upload_raises_when_a_concurrent_operation_already_won() -> None:
    uploads = _NeverTransitionsUploadRepository()
    chunks = InMemoryChunkRepository()
    session = _upload_session()
    await uploads.add_session(session)
    await chunks.record_chunk(
        UploadChunk(
            id=uuid4(),
            tenant_id=session.tenant_id,
            upload_id=session.id,
            index=0,
            size_bytes=session.declared_size,
            sha256="0" * 64,
        )
    )

    with pytest.raises(UploadStateError):
        await CompleteUploadUseCase(uploads, chunks, InMemoryFileRepository()).execute(
            session.id, session.owner_id
        )


async def test_abort_upload_rejects_an_already_completed_upload() -> None:
    uploads = InMemoryUploadRepository()
    session = _upload_session()
    session.status = UploadStatus.COMPLETED
    await uploads.add_session(session)

    with pytest.raises(UploadStateError):
        await AbortUploadUseCase(uploads, InMemoryObjectStorage()).execute(
            session.id, session.owner_id
        )


async def test_abort_upload_raises_when_a_concurrent_operation_already_won() -> None:
    uploads = _NeverTransitionsUploadRepository()
    session = _upload_session()
    await uploads.add_session(session)

    with pytest.raises(UploadStateError):
        await AbortUploadUseCase(uploads, InMemoryObjectStorage()).execute(
            session.id, session.owner_id
        )


async def test_delete_file_raises_when_a_concurrent_operation_already_won() -> None:
    files = _NeverTransitionsFileRepository()
    owner_id = uuid4()
    asset = FileAsset(tenant_id=uuid4(), owner_id=owner_id, status=FileStatus.AVAILABLE)
    await files.add_file(asset)

    with pytest.raises(FileNotAvailableError):
        await DeleteFileUseCase(files).execute(asset.id, owner_id, can_delete_all=False)


async def test_upload_chunk_cleans_up_staging_and_reraises_on_a_storage_move_failure() -> None:
    uploads = InMemoryUploadRepository()
    session = _upload_session()
    await uploads.add_session(session)
    storage = _MoveFailsObjectStorage()
    use_case = UploadChunkUseCase(uploads, InMemoryChunkRepository(), storage)

    async def _body() -> AsyncIterator[bytes]:
        yield b"0123456789"

    with pytest.raises(OSError, match="simulated move failure"):
        await use_case.execute(
            upload_id=session.id,
            owner_id=session.owner_id,
            index=0,
            body=_body(),
            declared_sha256=None,
            content_length=None,
        )
    assert storage.blobs == {}, "staging blob is cleaned up even though the move failed"
