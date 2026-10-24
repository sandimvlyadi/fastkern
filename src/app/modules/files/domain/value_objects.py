"""Value objects for the files module. Pure Python: no framework imports (domain layer rule).

Storage paths are built only from server-generated parts (UUIDs and fixed names). The client's
filename never reaches a path; `display_name()` sanitizes it for display/metadata only.
"""

import re
from dataclasses import dataclass
from enum import StrEnum

_PART_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")
_MAX_DISPLAY_NAME = 255


class Region(StrEnum):
    """Top-level storage roots. `public` is the only one any web-facing code may read."""

    TMP = "tmp"
    PRIVATE = "private"
    PUBLIC = "public"


class Visibility(StrEnum):
    PUBLIC = "PUBLIC"
    PRIVATE = "PRIVATE"


class FileKind(StrEnum):
    IMAGE = "IMAGE"
    DOCUMENT = "DOCUMENT"
    GENERIC = "GENERIC"


@dataclass(frozen=True, slots=True)
class StorageKey:
    """A relative location inside one storage region, e.g. `private/files/<uuid>/original`."""

    region: Region
    parts: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.parts:
            raise ValueError("StorageKey needs at least one path part")
        for part in self.parts:
            if not _PART_PATTERN.fullmatch(part) or ".." in part:
                raise ValueError(f"unsafe storage path part: {part!r}")

    def child(self, *parts: str) -> StorageKey:
        return StorageKey(self.region, (*self.parts, *parts))


def file_key(region: Region, file_id: object, *parts: str) -> StorageKey:
    """`<region>/files/<file_id>/<parts...>`: the layout every file artifact uses."""
    return StorageKey(region, ("files", str(file_id), *parts))


def upload_chunk_key(upload_id: object, index: int) -> StorageKey:
    return StorageKey(Region.TMP, ("uploads", str(upload_id), "chunks", f"{index:06d}.part"))


def upload_dir_key(upload_id: object) -> StorageKey:
    return StorageKey(Region.TMP, ("uploads", str(upload_id)))


def display_name(raw: str) -> str:
    """Basename only, control characters removed, bounded length. Display metadata only."""
    base = re.split(r"[\\/]", raw)[-1]
    cleaned = "".join(ch for ch in base if ch.isprintable()).strip()
    return (cleaned or "file")[:_MAX_DISPLAY_NAME]
