from datetime import datetime
from uuid import UUID

from pydantic import ConfigDict, Field

from app.modules.files.domain.value_objects import Visibility
from app.shared.application.dto import CamelModel

_EXAMPLE_UPLOAD_ID = "9c858901-8a57-4791-81fe-4c455b099bc1"
_EXAMPLE_FILE_ID = "9c858901-8a57-4791-81fe-4c455b099bc9"


class InitiateUploadRequest(CamelModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "filename": "holiday-photo.jpg",
                    "size": 52428800,
                    "chunkSize": 20971520,
                    "sha256": None,
                    "visibility": "PRIVATE",
                }
            ]
        }
    )

    filename: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0, description="Total size in bytes. Checked against the server limit.")
    chunk_size: int | None = Field(
        default=None,
        gt=0,
        description="Bytes per chunk except the last. Defaults to the server maximum.",
    )
    sha256: str | None = Field(
        default=None,
        pattern=r"^[A-Fa-f0-9]{64}$",
        description="Optional SHA-256 of the whole file, verified after assembly.",
    )
    visibility: Visibility = Visibility.PRIVATE


class UploadSessionResponse(CamelModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "uploadId": _EXAMPLE_UPLOAD_ID,
                    "status": "INITIATED",
                    "visibility": "PRIVATE",
                    "declaredSize": 52428800,
                    "chunkSize": 20971520,
                    "totalChunks": 3,
                    "receivedChunks": [],
                    "fileId": None,
                    "expiresAt": "2026-10-04T10:00:00Z",
                }
            ]
        }
    )

    upload_id: UUID
    status: str
    visibility: Visibility
    declared_size: int
    chunk_size: int
    total_chunks: int
    received_chunks: list[int] = Field(default_factory=list)
    file_id: UUID | None = None
    expires_at: datetime


class ChunkReceiptResponse(CamelModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "index": 0,
                    "sizeBytes": 20971520,
                    "sha256": "0" * 64,
                    "duplicate": False,
                }
            ]
        }
    )

    index: int
    size_bytes: int
    sha256: str
    duplicate: bool


class CompleteUploadResponse(CamelModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"uploadId": _EXAMPLE_UPLOAD_ID, "fileId": _EXAMPLE_FILE_ID, "status": "UPLOADED"}
            ]
        }
    )

    upload_id: UUID
    file_id: UUID
    status: str


class FileVariantResponse(CamelModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "name": "256",
                    "width": 256,
                    "height": 192,
                    "mimeType": "image/webp",
                    "url": f"/api/v1/public/files/{_EXAMPLE_FILE_ID}/256",
                }
            ]
        }
    )

    name: str
    width: int | None = None
    height: int | None = None
    mime_type: str
    url: str | None = None


class FileAssetResponse(CamelModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": _EXAMPLE_FILE_ID,
                    "status": "AVAILABLE",
                    "visibility": "PUBLIC",
                    "kind": "IMAGE",
                    "mimeType": "image/webp",
                    "sizeBytes": 1843200,
                    "sha256": "0" * 64,
                    "width": 4000,
                    "height": 3000,
                    "originalName": "holiday-photo.jpg",
                    "failureReason": None,
                    "variants": [],
                }
            ]
        }
    )

    id: UUID
    status: str
    visibility: Visibility
    kind: str
    mime_type: str
    size_bytes: int
    sha256: str
    width: int | None = None
    height: int | None = None
    original_name: str
    failure_reason: str | None = None
    variants: list[FileVariantResponse] = Field(default_factory=list)
