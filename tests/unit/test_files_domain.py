from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.modules.files.domain import signatures
from app.modules.files.domain.entities import UploadSession, total_chunks_for
from app.modules.files.domain.signatures import detect
from app.modules.files.domain.state import (
    FileStatus,
    UploadStatus,
    can_transition_file,
    can_transition_upload,
)
from app.modules.files.domain.value_objects import (
    FileKind,
    Region,
    StorageKey,
    display_name,
    file_key,
    upload_chunk_key,
)

PNG_HEAD = b"\x89PNG\r\n\x1a\n" + b"\x00" * 56
JPEG_HEAD = b"\xff\xd8\xff\xe0" + b"\x00" * 60
WEBP_HEAD = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 52


@pytest.mark.parametrize(
    ("src", "dst", "allowed"),
    [
        (UploadStatus.INITIATED, UploadStatus.UPLOADING, True),
        (UploadStatus.UPLOADING, UploadStatus.COMPLETED, True),
        (UploadStatus.INITIATED, UploadStatus.COMPLETED, False),
        (UploadStatus.COMPLETED, UploadStatus.ABORTED, False),
        (UploadStatus.ABORTED, UploadStatus.UPLOADING, False),
        (UploadStatus.EXPIRED, UploadStatus.UPLOADING, False),
    ],
)
def test_upload_state_machine(src: UploadStatus, dst: UploadStatus, allowed: bool) -> None:
    assert can_transition_upload(src, dst) is allowed


@pytest.mark.parametrize(
    ("src", "dst", "allowed"),
    [
        (FileStatus.UPLOADED, FileStatus.SCANNING, True),
        (FileStatus.SCANNING, FileStatus.PROCESSING, True),
        (FileStatus.SCANNING, FileStatus.AVAILABLE, True),  # documents and generic files
        (FileStatus.PROCESSING, FileStatus.AVAILABLE, True),
        (FileStatus.SCANNING, FileStatus.REJECTED, True),
        (FileStatus.UPLOADED, FileStatus.AVAILABLE, False),  # must be scanned first
        (FileStatus.AVAILABLE, FileStatus.DELETED, True),
        (FileStatus.REJECTED, FileStatus.AVAILABLE, False),  # terminal
        (FileStatus.FAILED, FileStatus.SCANNING, False),
    ],
)
def test_file_state_machine(src: FileStatus, dst: FileStatus, allowed: bool) -> None:
    assert can_transition_file(src, dst) is allowed


@pytest.mark.parametrize(
    "parts",
    [("../etc/passwd",), ("..",), ("a/../b",), ("",), (".hidden",), ("x" * 65,)],
)
def test_storage_key_rejects_unsafe_parts(parts: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        StorageKey(Region.PRIVATE, parts)


def test_storage_key_accepts_server_generated_parts() -> None:
    key = file_key(Region.PUBLIC, uuid4(), "variants", "256")
    assert key.region is Region.PUBLIC
    assert key.parts[0] == "files"
    assert upload_chunk_key(uuid4(), 3).parts[-1] == "000003.part"


def test_display_name_keeps_only_basename_and_printable_text() -> None:
    assert display_name("../../etc/evil\x00.png") == "evil.png"
    assert display_name("C:\\Users\\me\\photo.jpg") == "photo.jpg"
    assert display_name("   ") == "file"
    assert len(display_name("a" * 400)) == 255


def test_detect_known_images_and_document() -> None:
    assert detect(PNG_HEAD) == signatures.DetectedType(FileKind.IMAGE, "image/png", "PNG")
    assert detect(JPEG_HEAD).image_format == "JPEG"
    assert detect(WEBP_HEAD).mime_type == "image/webp"
    assert detect(b"%PDF-1.7\n").kind is FileKind.DOCUMENT


@pytest.mark.parametrize(
    "head",
    [
        b"MZ\x90\x00" + b"\x00" * 60,  # Windows executable
        b"\x7fELF" + b"\x00" * 60,  # Linux executable
        b"\xcf\xfa\xed\xfe" + b"\x00" * 60,  # Mach-O
        b"#!/bin/sh\nrm -rf /" + b"\x00" * 40,  # script
        b"  <!DOCTYPE html><html><script>alert(1)</script>",  # active markup
        b"\xef\xbb\xbf<svg xmlns='http://www.w3.org/2000/svg'><script/></svg>",  # SVG with BOM
        b"<?xml version='1.0'?><html>",
    ],
)
def test_detect_rejects_executables_and_active_markup(head: bytes) -> None:
    assert detect(head) is None


def test_detect_spoofed_image_header_is_not_an_image() -> None:
    # The client may claim image/png; the bytes are markup, so detection must not say image.
    assert detect(b"<html><body>not a png</body></html>") is None


def test_detect_unknown_binary_is_generic_octet_stream() -> None:
    detected = detect(b"\x01\x02\x03\x04" + b"\x10" * 60)
    assert detected is not None
    assert detected.kind is FileKind.GENERIC
    assert detected.mime_type == "application/octet-stream"


@pytest.mark.parametrize(
    ("size", "chunk", "expected"),
    [(1, 10, 1), (10, 10, 1), (11, 10, 2), (30, 10, 3), (31, 10, 4), (0, 10, 1)],
)
def test_total_chunks(size: int, chunk: int, expected: int) -> None:
    assert total_chunks_for(size, chunk) == expected


def _session(size: int, chunk: int, expires_at: datetime | None = None) -> UploadSession:
    return UploadSession(
        tenant_id=uuid4(),
        filename="f.bin",
        declared_size=size,
        chunk_size=chunk,
        total_chunks=total_chunks_for(size, chunk),
        expires_at=expires_at or datetime.now(UTC) + timedelta(hours=1),
    )


def test_expected_chunk_size_last_chunk_carries_remainder() -> None:
    session = _session(size=25, chunk=10)
    assert [session.expected_chunk_size(i) for i in range(3)] == [10, 10, 5]
    assert session.is_valid_index(2) and not session.is_valid_index(3)


def test_expiry_accepts_naive_datetimes_from_sqlite() -> None:
    naive_past = datetime(2020, 1, 1)  # SQLite returns naive values
    session = _session(size=5, chunk=5, expires_at=naive_past)
    assert session.is_expired(datetime.now(UTC))
