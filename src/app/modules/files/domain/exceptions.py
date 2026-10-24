"""Files-module errors. HTTP-facing ones are AppError subclasses with a locale `key`;
`FileRejectedError` is background-only and never reaches a response."""

from app.core.exceptions import (
    ConflictError,
    GoneError,
    NotFoundError,
    PayloadTooLargeError,
    ValidationAppError,
)


class UploadNotFoundError(NotFoundError):
    code = "upload_not_found"
    key = "files.upload_not_found"
    message = "Upload not found."


class FileAssetNotFoundError(NotFoundError):
    code = "file_not_found"
    key = "files.not_found"
    message = "File not found."


class UploadExpiredError(GoneError):
    code = "upload_expired"
    key = "files.upload_expired"
    message = "The upload session has expired."


class UploadStateError(ConflictError):
    code = "upload_state_invalid"
    key = "files.upload_state_invalid"
    message = "The upload is not in a state that allows this operation."


class InvalidUploadError(ValidationAppError):
    code = "invalid_upload"
    key = "files.invalid_upload"
    message = "The upload request is invalid."


class ChunkIndexError(ValidationAppError):
    code = "chunk_index_invalid"
    key = "files.chunk_index_invalid"
    message = "Chunk index is out of range for this upload."


class ChunkSizeMismatchError(ValidationAppError):
    code = "chunk_size_mismatch"
    key = "files.chunk_size_mismatch"
    message = "Chunk size does not match the upload's expected chunk size."


class ChunkTooLargeError(PayloadTooLargeError):
    code = "chunk_too_large"
    key = "files.chunk_too_large"
    message = "The chunk is larger than the expected chunk size."


class ChecksumMismatchError(ValidationAppError):
    code = "checksum_mismatch"
    key = "files.checksum_mismatch"
    message = "The checksum does not match the received data."


class ChunkConflictError(ConflictError):
    code = "chunk_conflict"
    key = "files.chunk_conflict"
    message = "This chunk was already received with different content."


class ChunksMissingError(ConflictError):
    code = "chunks_missing"
    key = "files.chunks_missing"
    message = "Some chunks have not been uploaded yet."


class TooManyActiveUploadsError(ConflictError):
    code = "too_many_active_uploads"
    key = "files.too_many_active_uploads"
    message = "Too many uploads are in progress. Finish or abort one before starting another."


class UploadTooLargeError(PayloadTooLargeError):
    code = "upload_too_large"
    key = "files.upload_too_large"
    message = "The file is larger than the maximum allowed size."


class SingleUploadTooLargeError(PayloadTooLargeError):
    """A single-request upload is limited to one chunk; larger files use the chunked endpoints."""

    code = "single_upload_too_large"
    key = "files.single_upload_too_large"
    message = "This file is too large for a single request. Use the chunked upload endpoints."


class FileNotAvailableError(ConflictError):
    code = "file_not_available"
    key = "files.not_available"
    message = "The file is not available yet."


class FileRejectedError(Exception):
    """Background pipeline rejection. `reason` is a stable code stored in `failure_reason`."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
