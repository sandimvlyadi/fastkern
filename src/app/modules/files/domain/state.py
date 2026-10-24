"""Lifecycle state machines for upload sessions and file assets (pure, no I/O).

Upload session (`UploadStatus`) is the TEMPORARY stage: chunks live on it and in `tmp/`.
File asset (`FileStatus`) starts at UPLOADED once the session is completed, and ends in
AVAILABLE (or REJECTED/FAILED/DELETED). Every status change in the database is a conditional
UPDATE from an expected status, so a re-run task cannot repeat a transition.
"""

from enum import StrEnum


class UploadStatus(StrEnum):
    INITIATED = "INITIATED"
    UPLOADING = "UPLOADING"
    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"
    EXPIRED = "EXPIRED"


class FileStatus(StrEnum):
    UPLOADED = "UPLOADED"
    SCANNING = "SCANNING"
    PROCESSING = "PROCESSING"
    AVAILABLE = "AVAILABLE"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    DELETED = "DELETED"


_UPLOAD_TRANSITIONS: dict[UploadStatus, frozenset[UploadStatus]] = {
    UploadStatus.INITIATED: frozenset(
        {UploadStatus.UPLOADING, UploadStatus.ABORTED, UploadStatus.EXPIRED}
    ),
    UploadStatus.UPLOADING: frozenset(
        {UploadStatus.COMPLETED, UploadStatus.ABORTED, UploadStatus.EXPIRED}
    ),
    UploadStatus.COMPLETED: frozenset(),
    UploadStatus.ABORTED: frozenset(),
    UploadStatus.EXPIRED: frozenset(),
}

_FILE_TRANSITIONS: dict[FileStatus, frozenset[FileStatus]] = {
    FileStatus.UPLOADED: frozenset({FileStatus.SCANNING, FileStatus.FAILED}),
    # DOCUMENT and GENERIC files skip PROCESSING.
    FileStatus.SCANNING: frozenset(
        {FileStatus.PROCESSING, FileStatus.AVAILABLE, FileStatus.REJECTED, FileStatus.FAILED}
    ),
    FileStatus.PROCESSING: frozenset({FileStatus.AVAILABLE, FileStatus.FAILED}),
    FileStatus.AVAILABLE: frozenset({FileStatus.DELETED}),
    FileStatus.REJECTED: frozenset(),
    FileStatus.FAILED: frozenset(),
    FileStatus.DELETED: frozenset(),
}

# Non-terminal file states that cleanup may fail when they go stale.
STALE_FILE_STATUSES: frozenset[FileStatus] = frozenset(
    {FileStatus.UPLOADED, FileStatus.SCANNING, FileStatus.PROCESSING}
)


def can_transition_upload(src: UploadStatus, dst: UploadStatus) -> bool:
    return dst in _UPLOAD_TRANSITIONS[src]


def can_transition_file(src: FileStatus, dst: FileStatus) -> bool:
    return dst in _FILE_TRANSITIONS[src]
