"""Files-module entities: plain dataclasses, no framework imports (import-linter enforced)."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from app.modules.files.domain.state import FileStatus, UploadStatus
from app.modules.files.domain.value_objects import FileKind, Visibility
from app.shared.domain.entity import TenantScopedEntity


@dataclass(kw_only=True)
class UploadSession(TenantScopedEntity):
    owner_id: UUID | None = None
    filename: str
    declared_size: int
    chunk_size: int
    total_chunks: int
    expected_sha256: str | None = None
    visibility: Visibility = Visibility.PRIVATE
    status: UploadStatus = UploadStatus.INITIATED
    expires_at: datetime
    file_id: UUID | None = None
    failure_reason: str | None = None

    def is_expired(self, now: datetime) -> bool:
        expires = self.expires_at if self.expires_at.tzinfo else self.expires_at.replace(tzinfo=UTC)
        return now >= expires

    def expected_chunk_size(self, index: int) -> int:
        """Every chunk is `chunk_size` except the last, which carries the remainder."""
        if index == self.total_chunks - 1:
            return self.declared_size - self.chunk_size * (self.total_chunks - 1)
        return self.chunk_size

    def is_valid_index(self, index: int) -> bool:
        return 0 <= index < self.total_chunks


def total_chunks_for(size: int, chunk_size: int) -> int:
    return max(1, -(-size // chunk_size))


@dataclass(kw_only=True)
class UploadChunk:
    id: UUID
    tenant_id: UUID
    upload_id: UUID
    index: int
    size_bytes: int
    sha256: str


@dataclass(kw_only=True)
class FileVariant:
    name: str
    width: int | None = None
    height: int | None = None
    size_bytes: int
    sha256: str
    mime_type: str


@dataclass(kw_only=True)
class FileAsset(TenantScopedEntity):
    owner_id: UUID | None = None
    status: FileStatus = FileStatus.UPLOADED
    visibility: Visibility = Visibility.PRIVATE
    kind: FileKind = FileKind.GENERIC
    mime_type: str = "application/octet-stream"
    size_bytes: int = 0
    sha256: str = ""
    width: int | None = None
    height: int | None = None
    original_name: str = "file"
    failure_reason: str | None = None
    variants: list[FileVariant] = field(default_factory=list)

    @property
    def is_public_available(self) -> bool:
        return self.visibility is Visibility.PUBLIC and self.status is FileStatus.AVAILABLE
