"""Offset and cursor pagination primitives, shared by every module.

Cursor pagination is opaque-cursor based (base64 of the last sort key) so
the encoding can change later without breaking API consumers.
"""

import base64
import binascii
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, Field


class OffsetPage[T](BaseModel):
    items: list[T]
    total: int
    page: int
    page_size: int

    @property
    def total_pages(self) -> int:
        return (self.total + self.page_size - 1) // self.page_size if self.page_size else 0

    def to_meta(self) -> dict[str, Any]:
        """The `meta.pagination` block every list endpoint returns — one
        definition so routers don't hand-roll this dict (and drift on
        field names) at each call site."""
        return {
            "page": self.page,
            "pageSize": self.page_size,
            "total": self.total,
            "totalPages": self.total_pages,
        }


MAX_PAGE = 1_000_000
"""Upper bound on `page`. Without one, `page=10**30` reaches Postgres as an OFFSET outside int64
(asyncpg `DataError` -> 500; SQLite silently accepts it, so the suite never saw it). With
`page_size <= 200` the largest offset is 2e8, far inside int64."""


class OffsetParams(BaseModel):
    page: int = Field(default=1, ge=1, le=MAX_PAGE)
    page_size: int = Field(default=20, ge=1, le=200)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


def paginate_sequence[T](items: Sequence[T], params: OffsetParams) -> OffsetPage[T]:
    """An `OffsetPage` over a fixed, in-memory list — for a code-defined catalog (e.g. the
    permission/scope catalogs) that still answers with the same paging shape as every
    DB-backed list endpoint."""
    return OffsetPage(
        items=list(items[params.offset : params.offset + params.page_size]),
        total=len(items),
        page=params.page,
        page_size=params.page_size,
    )


class CursorPage[T](BaseModel):
    items: list[T]
    next_cursor: str | None = None
    has_more: bool = False


class CursorParams(BaseModel):
    cursor: str | None = None
    limit: int = Field(default=20, ge=1, le=200)


def encode_cursor(value: Any) -> str:
    return base64.urlsafe_b64encode(str(value).encode()).decode()


def decode_cursor(cursor: str) -> str:
    try:
        return base64.urlsafe_b64decode(cursor.encode()).decode()
    except (binascii.Error, UnicodeDecodeError) as exc:
        from app.core.exceptions import ValidationAppError

        raise ValidationAppError("Invalid pagination cursor.") from exc
