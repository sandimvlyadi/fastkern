"""ORM tables for the files module. Follows the project's table/column naming standards."""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.infrastructure.orm_base import ORDER_PARENT, StandardModel, soft_delete_unique_index

CHUNK_UNIQUE_INDEX = "uq_trans_file_upload_chunks_upload_index"
VARIANT_UNIQUE_INDEX = "uq_trans_file_variants_file_name"


class FileUploadModel(StandardModel):
    """Upload session: the TEMPORARY stage. Owner is `created_by` (AuditMixin)."""

    __tablename__ = "trans_file_uploads"
    __table_args__ = (
        Index("ix_trans_file_uploads_tenant_owner_status", "tenant_id", "created_by", "status"),
        {"comment": "Resumable upload sessions (chunked upload lifecycle)."},
    )

    filename_display: Mapped[str] = mapped_column(
        String(255), comment="Sanitized display name supplied by the client; never used in a path."
    )
    declared_size: Mapped[int] = mapped_column(BigInteger, comment="Declared total size in bytes.")
    chunk_size: Mapped[int] = mapped_column(
        Integer, comment="Expected size of every chunk but the last."
    )
    total_chunks: Mapped[int] = mapped_column(
        Integer, comment="Number of chunks the upload is split into."
    )
    expected_sha256: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Optional client-declared SHA-256 of the whole file (hex).",
    )
    visibility: Mapped[str] = mapped_column(
        String(16), server_default="PRIVATE", comment="PUBLIC or PRIVATE, applied when processed."
    )
    status: Mapped[str] = mapped_column(
        String(16), server_default="INITIATED", comment="Upload session status (UploadStatus)."
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), comment="Session expiry (UTC); after this chunks are refused."
    )
    file_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("trans_files.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        sort_order=ORDER_PARENT,
        comment="The file asset created when the session was completed.",
    )
    failure_reason: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="Stable failure code, if the session failed."
    )


class FileUploadChunkModel(StandardModel):
    """One received chunk. The unique index is the race guard for concurrent identical chunks."""

    __tablename__ = "trans_file_upload_chunks"
    __table_args__ = (
        soft_delete_unique_index(CHUNK_UNIQUE_INDEX, "upload_id", "chunk_index"),
        Index("ix_trans_file_upload_chunks_tenant_upload", "tenant_id", "upload_id"),
        {"comment": "Chunks received for a resumable upload."},
    )

    upload_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trans_file_uploads.id", ondelete="CASCADE"),
        sort_order=ORDER_PARENT,
        comment="The upload session this chunk belongs to.",
    )
    chunk_index: Mapped[int] = mapped_column(Integer, comment="Zero-based chunk position.")
    size_bytes: Mapped[int] = mapped_column(BigInteger, comment="Bytes received for this chunk.")
    sha256: Mapped[str] = mapped_column(String(64), comment="SHA-256 of the chunk bytes (hex).")


class FileModel(StandardModel):
    """Final asset. The row id doubles as the storage key (never the client filename)."""

    __tablename__ = "trans_files"
    __table_args__ = (
        Index("ix_trans_files_tenant_visibility_status", "tenant_id", "visibility", "status"),
        {"comment": "Uploaded file assets (images, documents and generic files)."},
    )

    status: Mapped[str] = mapped_column(String(16), comment="File lifecycle status (FileStatus).")
    visibility: Mapped[str] = mapped_column(String(16), comment="PUBLIC or PRIVATE.")
    kind: Mapped[str] = mapped_column(String(16), comment="IMAGE, DOCUMENT or GENERIC.")
    mime_type: Mapped[str] = mapped_column(
        String(64),
        server_default="application/octet-stream",
        comment="MIME type detected from the content signature, never from the client.",
    )
    size_bytes: Mapped[int] = mapped_column(
        BigInteger, server_default="0", comment="Size of the stored source."
    )
    sha256: Mapped[str] = mapped_column(
        String(64), server_default="", comment="SHA-256 of the stored source."
    )
    width: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Image width in pixels."
    )
    height: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Image height in pixels."
    )
    original_name: Mapped[str] = mapped_column(
        String(255), server_default="file", comment="Sanitized original filename, display only."
    )
    failure_reason: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="Stable failure code when REJECTED or FAILED."
    )
    storage_purged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When cleanup removed the artifacts on disk; NULL while they still exist.",
    )


class FileVariantModel(StandardModel):
    """A stored representation of a file: `original` plus any generated image sizes."""

    __tablename__ = "trans_file_variants"
    __table_args__ = (
        soft_delete_unique_index(VARIANT_UNIQUE_INDEX, "file_id", "name"),
        Index("ix_trans_file_variants_tenant_file", "tenant_id", "file_id"),
        {"comment": "Stored variants (original and resized derivatives) of a file asset."},
    )

    file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trans_files.id", ondelete="CASCADE"),
        sort_order=ORDER_PARENT,
        comment="The file asset this variant belongs to.",
    )
    name: Mapped[str] = mapped_column(
        String(16), comment="Variant name: original or the pixel size."
    )
    width: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Width in pixels, images only."
    )
    height: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Height in pixels, images only."
    )
    size_bytes: Mapped[int] = mapped_column(BigInteger, comment="Stored size in bytes.")
    sha256: Mapped[str] = mapped_column(String(64), comment="SHA-256 of the stored variant (hex).")
    mime_type: Mapped[str] = mapped_column(String(64), comment="MIME type served for this variant.")
