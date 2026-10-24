"""Ports the files application layer depends on. Infrastructure provides the implementations.

`ObjectStoragePort` is deliberately path-free for its callers: they pass `StorageKey`s. A
filesystem adapter maps them to disk today; an S3/MinIO adapter could implement the same port.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from app.modules.files.domain.entities import FileAsset, FileVariant, UploadChunk, UploadSession
from app.modules.files.domain.state import FileStatus, UploadStatus
from app.modules.files.domain.value_objects import StorageKey


class ObjectStoragePort(ABC):
    @abstractmethod
    async def write(self, key: StorageKey, source: AsyncIterator[bytes]) -> None:
        """Atomically write `source` to `key` (readers never see a partial file). On failure
        nothing is left behind at `key`."""

    @abstractmethod
    def read(self, key: StorageKey, *, chunk_size: int = 1024 * 1024) -> AsyncGenerator[bytes]:
        """Streamed read. Generators must be closed (`aclosing`) when a caller stops early."""

    @abstractmethod
    async def exists(self, key: StorageKey) -> bool: ...

    @abstractmethod
    async def move(self, source: StorageKey, destination: StorageKey) -> None:
        """Atomic rename within the storage (same filesystem)."""

    @abstractmethod
    async def concatenate(
        self, sources: Sequence[StorageKey], destination: StorageKey
    ) -> tuple[int, str]:
        """Join `sources` in order into `destination`; returns (size_bytes, sha256_hex)."""

    @abstractmethod
    async def delete_tree(self, key: StorageKey) -> None:
        """Remove a file or directory tree. Idempotent: missing targets are not an error."""

    @abstractmethod
    async def list_children(self, key: StorageKey) -> list[str]:
        """Names directly under `key` (empty when it does not exist)."""

    @abstractmethod
    async def modified_at(self, key: StorageKey) -> datetime | None:
        """Last modification time (UTC) of `key`, or None when it does not exist."""

    @abstractmethod
    def materialize(self, key: StorageKey) -> AbstractAsyncContextManager[Path]:
        """Yield a local filesystem path for tools that need one (Pillow, clamd). Remote
        adapters download to a temp file and remove it on exit."""


@dataclass(frozen=True, slots=True)
class ScanResult:
    clean: bool
    signature: str | None = None


class ScannerUnavailableError(Exception):
    """The malware scanner could not answer; the file is not marked clean."""


class MalwareScannerPort(ABC):
    @abstractmethod
    async def scan(self, path: Path) -> ScanResult: ...

    @abstractmethod
    async def self_test(self) -> None:
        """Raise ScannerUnavailableError unless the scanner can work right now."""


@dataclass(frozen=True, slots=True)
class ImageOptions:
    variant_sizes: tuple[int, ...]
    max_dimension: int
    quality: int
    max_pixels: int


@dataclass(frozen=True, slots=True)
class ProcessedOutput:
    name: str
    path: Path
    width: int
    height: int
    size_bytes: int
    sha256: str
    mime_type: str


@dataclass(frozen=True, slots=True)
class ImageResult:
    width: int
    height: int
    outputs: list[ProcessedOutput] = field(default_factory=list)


class ImageProcessorPort(ABC):
    @abstractmethod
    def process(
        self, source: Path, out_dir: Path, options: ImageOptions, image_format: str
    ) -> ImageResult:
        """Synchronous (CPU-bound). Raises FileRejectedError("INVALID_IMAGE") for undecodable or
        oversized input and FileRejectedError("TYPE_MISMATCH") when the decoded format differs
        from the detected signature."""


class ChunkAlreadyReceivedError(Exception):
    """Raised by `UploadRepositoryPort.record_chunk` when the (upload, index) pair exists."""


class UploadRepositoryPort(ABC):
    @abstractmethod
    async def add_session(self, session: UploadSession) -> UploadSession: ...

    @abstractmethod
    async def get_owned_session(self, upload_id: UUID, owner_id: UUID) -> UploadSession | None:
        """Tenant-scoped lookup restricted to the caller's own session."""

    @abstractmethod
    async def get_session_unscoped(self, upload_id: UUID) -> UploadSession | None:
        """Background tasks and cleanup only (no request tenant)."""

    @abstractmethod
    async def count_active_sessions(self, owner_id: UUID, now: datetime) -> int: ...

    @abstractmethod
    async def transition_session(
        self, upload_id: UUID, expected: UploadStatus, new: UploadStatus, **fields: Any
    ) -> bool:
        """Conditional UPDATE: True only when the row was in `expected` and is now `new`."""

    @abstractmethod
    async def list_sessions_by_status(
        self, statuses: Sequence[UploadStatus]
    ) -> list[UploadSession]:
        """Cleanup only (no request tenant)."""


class ChunkRepositoryPort(ABC):
    @abstractmethod
    async def record_chunk(self, chunk: UploadChunk) -> UploadChunk:
        """Raises ChunkAlreadyReceivedError when (upload, index) already exists."""

    @abstractmethod
    async def get_chunk(self, upload_id: UUID, index: int) -> UploadChunk | None: ...

    @abstractmethod
    async def list_chunks(self, upload_id: UUID, tenant_id: UUID) -> list[UploadChunk]:
        """Explicit tenant: the background worker has no request context."""


class FileRepositoryPort(ABC):
    @abstractmethod
    async def add_file(self, asset: FileAsset) -> FileAsset: ...

    @abstractmethod
    async def get_owned_file(self, file_id: UUID) -> FileAsset | None:
        """Tenant-scoped lookup (request context)."""

    @abstractmethod
    async def get_file_unscoped(self, file_id: UUID) -> FileAsset | None:
        """Background tasks and cleanup only (no request tenant)."""

    @abstractmethod
    async def transition_file(
        self, file_id: UUID, expected: FileStatus, new: FileStatus, **fields: Any
    ) -> bool:
        """Conditional UPDATE, see `UploadRepositoryPort.transition_session`."""

    @abstractmethod
    async def add_variants(
        self, file_id: UUID, tenant_id: UUID, variants: list[FileVariant]
    ) -> None:
        """Explicit tenant: the background worker has no request context."""

    @abstractmethod
    async def list_variants(
        self, file_id: UUID, tenant_id: UUID | None = None
    ) -> list[FileVariant]:
        """`tenant_id` is only passed by anonymous (public) reads, which have no request tenant."""

    @abstractmethod
    async def list_files_by_status(self, statuses: Sequence[FileStatus]) -> list[FileAsset]:
        """Cleanup only (no request tenant)."""

    @abstractmethod
    async def list_unpurged_files(self, statuses: Sequence[FileStatus]) -> list[FileAsset]:
        """Cleanup only: files whose artifacts still exist on disk (storage_purged_at is NULL)."""

    @abstractmethod
    async def mark_storage_purged(self, file_id: UUID, now: datetime) -> None:
        """Cleanup only: record that the artifacts were removed, so later runs skip the file."""
