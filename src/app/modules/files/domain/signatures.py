"""Content detection by magic bytes. Client Content-Type and extension are never trusted.

Two decisions:
* Known images (JPEG/PNG/GIF/WebP) and PDF are classified positively.
* Any other type (GENERIC) cannot be allow-listed by signature, so it is accepted unless its
  content is active or executable (deny-list below). Such files are always served as attachments.
"""

from typing import NamedTuple

from app.modules.files.domain.value_objects import FileKind


class DetectedType(NamedTuple):
    kind: FileKind
    mime_type: str
    image_format: str | None = None  # Pillow format name for IMAGE kinds ("JPEG", "PNG", ...)


_IMAGE_SIGNATURES: tuple[tuple[bytes, str, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg", "JPEG"),
    (b"\x89PNG\r\n\x1a\n", "image/png", "PNG"),
    (b"GIF87a", "image/gif", "GIF"),
    (b"GIF89a", "image/gif", "GIF"),
)

# Executables and bytecode. Checked before anything else is allowed through as GENERIC.
_EXECUTABLE_SIGNATURES: tuple[bytes, ...] = (
    b"MZ",  # Windows PE
    b"\x7fELF",  # Linux ELF
    b"\xfe\xed\xfa\xce",  # Mach-O 32-bit (big endian)
    b"\xfe\xed\xfa\xcf",  # Mach-O 64-bit (big endian)
    b"\xce\xfa\xed\xfe",  # Mach-O 32-bit (little endian)
    b"\xcf\xfa\xed\xfe",  # Mach-O 64-bit (little endian)
    b"\xca\xfe\xba\xbe",  # Java class file / Mach-O universal binary
    b"#!",  # scripts
)

# Markup a browser renders actively. Matched case-insensitively after leading whitespace.
_ACTIVE_MARKUP_PREFIXES: tuple[bytes, ...] = (
    b"<!doctype html",
    b"<html",
    b"<head",
    b"<body",
    b"<script",
    b"<svg",
    b"<?xml",
)


def detect(head: bytes) -> DetectedType | None:
    """Classify the first bytes of a file. Returns None when the content is rejected.

    `head` should be at least 64 bytes (or the whole file if smaller).
    """
    for signature, mime, image_format in _IMAGE_SIGNATURES:
        if head.startswith(signature):
            return DetectedType(FileKind.IMAGE, mime, image_format)
    if head[0:4] == b"RIFF" and head[8:12] == b"WEBP":
        return DetectedType(FileKind.IMAGE, "image/webp", "WEBP")
    if head.startswith(b"%PDF-"):
        return DetectedType(FileKind.DOCUMENT, "application/pdf")
    if any(head.startswith(sig) for sig in _EXECUTABLE_SIGNATURES):
        return None
    if _looks_like_active_markup(head):
        return None
    return DetectedType(FileKind.GENERIC, "application/octet-stream")


def _looks_like_active_markup(head: bytes) -> bool:
    stripped = head.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    return any(stripped.startswith(prefix) for prefix in _ACTIVE_MARKUP_PREFIXES)
