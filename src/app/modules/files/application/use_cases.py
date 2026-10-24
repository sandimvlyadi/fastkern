"""Use cases for the files module: one class per use case.

Authorization: an owner (the user who created the upload or file) can always act on their own
resources. Users holding the relevant `files:*` permission can act on any file in their tenant.
Anyone else gets NotFound, not Forbidden, so resource existence is not leaked (anti-IDOR).
"""

import hashlib
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.modules.files.domain.entities import (
    FileAsset,
    FileVariant,
    UploadChunk,
    UploadSession,
    total_chunks_for,
)
from app.modules.files.domain.exceptions import (
    ChecksumMismatchError,
    ChunkConflictError,
    ChunkIndexError,
    ChunkSizeMismatchError,
    ChunksMissingError,
    ChunkTooLargeError,
    FileAssetNotFoundError,
    FileNotAvailableError,
    InvalidUploadError,
    SingleUploadTooLargeError,
    TooManyActiveUploadsError,
    UploadExpiredError,
    UploadNotFoundError,
    UploadStateError,
    UploadTooLargeError,
)
from app.modules.files.domain.ports import (
    ChunkAlreadyReceivedError,
    ChunkRepositoryPort,
    FileRepositoryPort,
    ObjectStoragePort,
    UploadRepositoryPort,
)
from app.modules.files.domain.state import STALE_FILE_STATUSES, FileStatus, UploadStatus
from app.modules.files.domain.value_objects import (
    FileKind,
    Region,
    StorageKey,
    Visibility,
    display_name,
    file_key,
    upload_chunk_key,
    upload_dir_key,
)
from app.shared.domain.identifiers import uuid7

_OPEN_UPLOAD_STATUSES: tuple[UploadStatus, ...] = (UploadStatus.INITIATED, UploadStatus.UPLOADING)


def utc_now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; everything stored is UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class UploadPolicy:
    chunk_max_bytes: int
    max_file_bytes: int
    session_ttl: timedelta
    max_active_sessions: int


@dataclass(frozen=True, slots=True)
class InitiateUploadInput:
    filename: str
    size: int
    chunk_size: int | None
    sha256: str | None
    visibility: Visibility


@dataclass(frozen=True, slots=True)
class ChunkReceipt:
    index: int
    size_bytes: int
    sha256: str
    duplicate: bool


@dataclass(frozen=True, slots=True)
class CleanupReport:
    expired_sessions: int = 0
    purged_upload_dirs: int = 0
    purged_file_trees: int = 0
    failed_stale_files: int = 0


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(ch in "0123456789abcdefABCDEF" for ch in value)


def _is_uuid(value: str) -> bool:
    try:
        UUID(value)
    except ValueError:
        return False
    return True


class _StreamStats:
    def __init__(self) -> None:
        self.size = 0
        self._digest = hashlib.sha256()

    def add(self, block: bytes) -> None:
        self.size += len(block)
        self._digest.update(block)

    @property
    def sha256(self) -> str:
        return self._digest.hexdigest()


async def _bounded(
    body: AsyncIterator[bytes], limit: int, stats: _StreamStats
) -> AsyncIterator[bytes]:
    """Counts the real bytes as they stream past and stops at `limit + 1`. Content-Length is
    only a fast-reject hint; the byte count here is what counts."""
    async for block in body:
        stats.add(block)
        if stats.size > limit:
            raise ChunkTooLargeError()
        yield block


def _ensure_open(session: UploadSession, now: datetime) -> None:
    if session.status is UploadStatus.EXPIRED or (
        session.status in _OPEN_UPLOAD_STATUSES and session.is_expired(now)
    ):
        raise UploadExpiredError()
    if session.status not in _OPEN_UPLOAD_STATUSES:
        raise UploadStateError()


def _pick_variant(variants: list[FileVariant], name: str) -> FileVariant:
    for variant in variants:
        if variant.name == name:
            return variant
    raise FileAssetNotFoundError()


class InitiateUploadUseCase:
    def __init__(
        self,
        uploads: UploadRepositoryPort,
        policy: UploadPolicy,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._uploads = uploads
        self._policy = policy
        self._now = now

    async def execute(
        self, data: InitiateUploadInput, *, tenant_id: UUID, owner_id: UUID
    ) -> UploadSession:
        if data.size <= 0:
            raise InvalidUploadError(reason="empty_file")
        if data.size > self._policy.max_file_bytes:
            raise UploadTooLargeError(max_bytes=self._policy.max_file_bytes)
        chunk_size = data.chunk_size or self._policy.chunk_max_bytes
        if not 0 < chunk_size <= self._policy.chunk_max_bytes:
            raise InvalidUploadError(reason="chunk_size", max_bytes=self._policy.chunk_max_bytes)
        if data.sha256 is not None and not _is_sha256(data.sha256):
            raise InvalidUploadError(reason="sha256")

        now = self._now()
        if (
            await self._uploads.count_active_sessions(owner_id, now)
            >= self._policy.max_active_sessions
        ):
            raise TooManyActiveUploadsError(max_active=self._policy.max_active_sessions)

        session = UploadSession(
            tenant_id=tenant_id,
            owner_id=owner_id,
            filename=display_name(data.filename),
            declared_size=data.size,
            chunk_size=chunk_size,
            total_chunks=total_chunks_for(data.size, chunk_size),
            expected_sha256=data.sha256.lower() if data.sha256 else None,
            visibility=data.visibility,
            status=UploadStatus.INITIATED,
            expires_at=now + self._policy.session_ttl,
        )
        return await self._uploads.add_session(session)


class GetUploadStatusUseCase:
    def __init__(self, uploads: UploadRepositoryPort, chunks: ChunkRepositoryPort) -> None:
        self._uploads = uploads
        self._chunks = chunks

    async def execute(self, upload_id: UUID, owner_id: UUID) -> tuple[UploadSession, list[int]]:
        session = await self._uploads.get_owned_session(upload_id, owner_id)
        if session is None:
            raise UploadNotFoundError()
        received = await self._chunks.list_chunks(upload_id, session.tenant_id)
        return session, sorted(chunk.index for chunk in received)


class UploadChunkUseCase:
    """Stream one chunk to staging, verify it, record it (the unique index decides races), then
    rename it into place last, so a crash mid-write never leaves a partial chunk visible."""

    def __init__(
        self,
        uploads: UploadRepositoryPort,
        chunks: ChunkRepositoryPort,
        storage: ObjectStoragePort,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._uploads = uploads
        self._chunks = chunks
        self._storage = storage
        self._now = now

    async def execute(
        self,
        *,
        upload_id: UUID,
        owner_id: UUID,
        index: int,
        body: AsyncIterator[bytes],
        declared_sha256: str | None,
        content_length: int | None,
    ) -> ChunkReceipt:
        session = await self._uploads.get_owned_session(upload_id, owner_id)
        if session is None:
            raise UploadNotFoundError()
        _ensure_open(session, self._now())
        if not session.is_valid_index(index):
            raise ChunkIndexError()
        expected = session.expected_chunk_size(index)
        if content_length is not None and content_length > expected:
            raise ChunkTooLargeError()
        if declared_sha256 is not None and not _is_sha256(declared_sha256):
            raise ChecksumMismatchError()
        if declared_sha256 is not None:
            # A retry of a chunk already stored: answer without reading the body again. The stored
            # chunk is the verified one, so nothing is overwritten.
            existing = await self._chunks.get_chunk(upload_id, index)
            if (
                existing is not None
                and existing.sha256 == declared_sha256.lower()
                and existing.size_bytes == expected
            ):
                return ChunkReceipt(index, existing.size_bytes, existing.sha256, duplicate=True)

        staging = StorageKey(
            Region.TMP, ("uploads", str(upload_id), "staging", f"{uuid.uuid4().hex}.part")
        )
        stats = _StreamStats()
        await self._storage.write(staging, _bounded(body, expected, stats))  # cleans up on failure
        if stats.size != expected:
            await self._storage.delete_tree(staging)
            raise ChunkSizeMismatchError(expected=expected, received=stats.size)
        if declared_sha256 is not None and declared_sha256.lower() != stats.sha256:
            await self._storage.delete_tree(staging)
            raise ChecksumMismatchError()

        try:
            await self._chunks.record_chunk(
                UploadChunk(
                    id=uuid7(),
                    tenant_id=session.tenant_id,
                    upload_id=upload_id,
                    index=index,
                    size_bytes=stats.size,
                    sha256=stats.sha256,
                )
            )
        except ChunkAlreadyReceivedError:
            await self._storage.delete_tree(staging)
            existing = await self._chunks.get_chunk(upload_id, index)
            if existing is not None and existing.sha256 == stats.sha256:
                return ChunkReceipt(index, stats.size, stats.sha256, duplicate=True)
            raise ChunkConflictError() from None

        try:
            await self._storage.move(staging, upload_chunk_key(upload_id, index))
        except BaseException:
            await self._storage.delete_tree(staging)
            raise
        await self._uploads.transition_session(
            upload_id, UploadStatus.INITIATED, UploadStatus.UPLOADING
        )
        return ChunkReceipt(index, stats.size, stats.sha256, duplicate=False)


class CompleteUploadUseCase:
    """Verifies every chunk is present and creates the file asset. Assembly, scanning and
    processing run later in the worker (`files.process_upload`), so this request returns quickly."""

    def __init__(
        self,
        uploads: UploadRepositoryPort,
        chunks: ChunkRepositoryPort,
        files: FileRepositoryPort,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._uploads = uploads
        self._chunks = chunks
        self._files = files
        self._now = now

    async def execute(self, upload_id: UUID, owner_id: UUID) -> tuple[UploadSession, UUID, bool]:
        """Returns (session, file_id, newly_completed). Only the first call is newly completed, and
        only that call may dispatch processing."""
        session = await self._uploads.get_owned_session(upload_id, owner_id)
        if session is None:
            raise UploadNotFoundError()
        if session.status is UploadStatus.COMPLETED and session.file_id is not None:
            return session, session.file_id, False  # idempotent retry of a completed upload
        _ensure_open(session, self._now())

        received = {
            chunk.index for chunk in await self._chunks.list_chunks(upload_id, session.tenant_id)
        }
        missing = [index for index in range(session.total_chunks) if index not in received]
        if missing:
            raise ChunksMissingError(missing=missing)

        asset = await self._files.add_file(
            FileAsset(
                tenant_id=session.tenant_id,
                owner_id=owner_id,
                status=FileStatus.UPLOADED,
                visibility=session.visibility,
                kind=FileKind.GENERIC,  # decided by content detection in the worker
                size_bytes=session.declared_size,
                original_name=session.filename,
            )
        )
        moved = await self._uploads.transition_session(
            upload_id, session.status, UploadStatus.COMPLETED, file_id=asset.id
        )
        if not moved:  # a concurrent abort or complete won the race; this request rolls back
            raise UploadStateError()
        return replace(session, status=UploadStatus.COMPLETED, file_id=asset.id), asset.id, True


class UploadSingleFileUseCase:
    """A whole file in one request: one session, one chunk, then complete.

    Composes the chunked use cases, so the checks and the race handling are the same ones the
    chunk endpoints use. Only files up to one chunk qualify.
    """

    def __init__(
        self,
        uploads: UploadRepositoryPort,
        chunks: ChunkRepositoryPort,
        files: FileRepositoryPort,
        storage: ObjectStoragePort,
        policy: UploadPolicy,
    ) -> None:
        self._uploads = uploads
        self._chunks = chunks
        self._files = files
        self._storage = storage
        self._policy = policy

    async def execute(
        self,
        *,
        filename: str,
        size: int,
        sha256: str | None,
        visibility: Visibility,
        body: AsyncIterator[bytes],
        tenant_id: UUID,
        owner_id: UUID,
    ) -> tuple[UUID, UUID, bool]:
        """Returns (upload_id, file_id, newly_completed)."""
        if size > self._policy.chunk_max_bytes:
            raise SingleUploadTooLargeError(max_bytes=self._policy.chunk_max_bytes)
        upload = await InitiateUploadUseCase(self._uploads, self._policy).execute(
            InitiateUploadInput(
                filename=filename,
                size=size,
                chunk_size=size,  # one chunk: the whole file
                sha256=sha256 or None,
                visibility=visibility,
            ),
            tenant_id=tenant_id,
            owner_id=owner_id,
        )
        await UploadChunkUseCase(self._uploads, self._chunks, self._storage).execute(
            upload_id=upload.id,
            owner_id=owner_id,
            index=0,
            body=body,
            declared_sha256=sha256 or None,
            content_length=size,
        )
        _, file_id, newly_completed = await CompleteUploadUseCase(
            self._uploads, self._chunks, self._files
        ).execute(upload.id, owner_id)
        return upload.id, file_id, newly_completed


class AbortUploadUseCase:
    def __init__(self, uploads: UploadRepositoryPort, storage: ObjectStoragePort) -> None:
        self._uploads = uploads
        self._storage = storage

    async def execute(self, upload_id: UUID, owner_id: UUID) -> bool:
        """True when this call aborted the upload; False when it was already aborted or expired."""
        session = await self._uploads.get_owned_session(upload_id, owner_id)
        if session is None:
            raise UploadNotFoundError()
        if session.status in (UploadStatus.ABORTED, UploadStatus.EXPIRED):
            return False
        if session.status is UploadStatus.COMPLETED:
            raise UploadStateError()
        moved = await self._uploads.transition_session(
            upload_id, session.status, UploadStatus.ABORTED
        )
        if not moved:
            raise UploadStateError()
        await self._storage.delete_tree(upload_dir_key(upload_id))
        return True


class GetFileUseCase:
    def __init__(self, files: FileRepositoryPort) -> None:
        self._files = files

    async def execute(
        self, file_id: UUID, caller_id: UUID, *, can_read_all: bool
    ) -> tuple[FileAsset, list[FileVariant]]:
        asset = await self._files.get_owned_file(file_id)
        if asset is None or not (can_read_all or asset.owner_id == caller_id):
            raise FileAssetNotFoundError()
        return asset, await self._files.list_variants(file_id)

    async def resolve_variant(
        self, file_id: UUID, caller_id: UUID, *, can_read_all: bool, variant_name: str
    ) -> tuple[FileAsset, FileVariant]:
        asset, variants = await self.execute(file_id, caller_id, can_read_all=can_read_all)
        if asset.status is not FileStatus.AVAILABLE:
            raise FileNotAvailableError()
        return asset, _pick_variant(variants, variant_name)


class ResolvePublicVariantUseCase:
    """Anonymous access: only PUBLIC + AVAILABLE files, and only their stored variants."""

    def __init__(self, files: FileRepositoryPort) -> None:
        self._files = files

    async def execute(self, file_id: UUID, variant_name: str) -> tuple[FileAsset, FileVariant]:
        asset = await self._files.get_file_unscoped(file_id)
        if asset is None or not asset.is_public_available:
            raise FileAssetNotFoundError()
        variants = await self._files.list_variants(file_id, tenant_id=asset.tenant_id)
        return asset, _pick_variant(variants, variant_name)


class DeleteFileUseCase:
    """Soft delete. Disk removal happens in the cleanup job, so the link stops working at once."""

    def __init__(self, files: FileRepositoryPort, now: Callable[[], datetime] = utc_now) -> None:
        self._files = files
        self._now = now

    async def execute(self, file_id: UUID, caller_id: UUID, *, can_delete_all: bool) -> None:
        asset = await self._files.get_owned_file(file_id)
        if asset is None or not (can_delete_all or asset.owner_id == caller_id):
            raise FileAssetNotFoundError()
        if asset.status is not FileStatus.AVAILABLE:
            raise FileNotAvailableError()
        moved = await self._files.transition_file(
            file_id,
            FileStatus.AVAILABLE,
            FileStatus.DELETED,
            deleted_at=self._now(),
            deleted_by=caller_id,
        )
        if not moved:
            raise FileNotAvailableError()


class CleanupFilesUseCase:
    """Idempotent sweep. Every step is safe to run twice, and a missing path is not an error.

    Work per run is limited to what is still pending: open sessions, upload directories, and file
    artifacts not yet marked purged (`storage_purged_at`). A finished file is purged once and then
    skipped, so a run does not rescan the whole history.

    An upload directory is removed only when no open session owns it AND it has not been touched for
    `stale_after`. A COMPLETED session's chunks are still read by the worker until the file is
    processed, so they must not be removed while that is in progress.
    """

    def __init__(
        self,
        uploads: UploadRepositoryPort,
        files: FileRepositoryPort,
        storage: ObjectStoragePort,
        now: Callable[[], datetime] = utc_now,
        stale_after: timedelta = timedelta(hours=2),
    ) -> None:
        self._uploads = uploads
        self._files = files
        self._storage = storage
        self._now = now
        self._stale_after = stale_after

    async def execute(self) -> CleanupReport:
        now = self._now()
        expired = await self._expire_sessions(now)
        upload_dirs = await self._purge_orphan_upload_dirs(now)
        stale = await self._fail_stale_files(now)
        trees = await self._purge_finished_files(now)
        return CleanupReport(
            expired_sessions=expired,
            purged_upload_dirs=upload_dirs,
            purged_file_trees=trees,
            failed_stale_files=stale,
        )

    async def _expire_sessions(self, now: datetime) -> int:
        expired = 0
        for session in await self._uploads.list_sessions_by_status(_OPEN_UPLOAD_STATUSES):
            if session.is_expired(now) and await self._uploads.transition_session(
                session.id, session.status, UploadStatus.EXPIRED
            ):
                expired += 1
        return expired

    async def _purge_orphan_upload_dirs(self, now: datetime) -> int:
        root = StorageKey(Region.TMP, ("uploads",))
        # Listing first is deliberate: a directory that exists was created after its session
        # was committed, so the open-session read below always sees that session as owned.
        names = await self._storage.list_children(root)
        open_ids = {
            str(s.id) for s in await self._uploads.list_sessions_by_status(_OPEN_UPLOAD_STATUSES)
        }
        purged = 0
        for name in names:
            if name in open_ids or not _is_uuid(name):
                continue
            modified = await self._storage.modified_at(upload_dir_key(name))
            if modified is None or _aware(modified) > now - self._stale_after:
                continue
            await self._storage.delete_tree(upload_dir_key(name))
            purged += 1
        return purged

    async def _fail_stale_files(self, now: datetime) -> int:
        cutoff = now - self._stale_after
        failed = 0
        for asset in await self._files.list_files_by_status(sorted(STALE_FILE_STATUSES)):
            is_stale = asset.created_at is not None and _aware(asset.created_at) < cutoff
            if is_stale and await self._files.transition_file(
                asset.id, asset.status, FileStatus.FAILED, failure_reason="STALE"
            ):
                failed += 1
        return failed

    async def _purge_finished_files(self, now: datetime) -> int:
        purged = 0
        pending = await self._files.list_unpurged_files(
            (FileStatus.FAILED, FileStatus.REJECTED, FileStatus.DELETED)
        )
        for asset in pending:
            for region in (Region.PRIVATE, Region.PUBLIC):
                await self._storage.delete_tree(file_key(region, asset.id))
            await self._files.mark_storage_purged(asset.id, now)
            purged += 1
        return purged
