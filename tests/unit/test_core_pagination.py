import pytest
from pydantic import ValidationError

from app.core.exceptions import ValidationAppError
from app.core.pagination import (
    CursorPage,
    CursorParams,
    OffsetPage,
    OffsetParams,
    decode_cursor,
    encode_cursor,
    paginate_sequence,
)
from app.core.response import PaginationMeta


def test_total_pages_exact_multiple() -> None:
    page = OffsetPage(items=[], total=40, page=1, page_size=20)
    assert page.total_pages == 2


def test_total_pages_remainder() -> None:
    page = OffsetPage(items=[], total=41, page=1, page_size=20)
    assert page.total_pages == 3


def test_total_pages_zero_total() -> None:
    page = OffsetPage(items=[], total=0, page=1, page_size=20)
    assert page.total_pages == 0


def test_total_pages_zero_page_size() -> None:
    page = OffsetPage(items=[], total=10, page=1, page_size=0)
    assert page.total_pages == 0


def test_to_meta_uses_camel_case_keys() -> None:
    page = OffsetPage(items=[1, 2], total=2, page=1, page_size=20)
    assert page.to_meta() == {"page": 1, "pageSize": 20, "total": 2, "totalPages": 1}


def test_pagination_meta_schema_matches_to_meta_output() -> None:
    """Regression guard: `PaginationMeta` (`app.core.response`), the
    `response_model` every list endpoint's OpenAPI docs use to describe
    `meta.pagination`, must stay in sync with the dict `to_meta()` actually
    produces at runtime. If this fails, either `to_meta()` changed shape or
    `PaginationMeta`'s fields/aliases need updating to match — otherwise
    `/docs` documents a shape the API doesn't actually return."""
    page = OffsetPage(items=[1, 2], total=2, page=1, page_size=20)
    meta_dict = page.to_meta()

    schema_keys = set(PaginationMeta.model_json_schema(by_alias=True)["properties"])
    assert set(meta_dict) == schema_keys

    validated = PaginationMeta.model_validate(meta_dict)
    assert validated.model_dump(by_alias=True) == meta_dict


def test_offset_params_offset_calculation() -> None:
    assert OffsetParams(page=1, page_size=20).offset == 0
    assert OffsetParams(page=3, page_size=20).offset == 40


def test_offset_params_rejects_page_below_one() -> None:
    with pytest.raises(ValidationError):
        OffsetParams(page=0)


def test_offset_params_rejects_page_size_above_max() -> None:
    with pytest.raises(ValidationError):
        OffsetParams(page_size=201)


def test_paginate_sequence_slices_and_reports_the_full_total() -> None:
    page = paginate_sequence(list(range(7)), OffsetParams(page=2, page_size=3))
    assert page.items == [3, 4, 5]
    assert (page.total, page.page, page.page_size, page.total_pages) == (7, 2, 3, 3)


def test_paginate_sequence_past_the_end_is_empty() -> None:
    page = paginate_sequence((1, 2), OffsetParams(page=5, page_size=10))
    assert page.items == []
    assert page.total == 2


def test_cursor_page_defaults() -> None:
    page: CursorPage[int] = CursorPage(items=[1])
    assert page.next_cursor is None
    assert page.has_more is False


def test_cursor_params_defaults() -> None:
    params = CursorParams()
    assert params.cursor is None
    assert params.limit == 20


def test_encode_decode_cursor_roundtrip() -> None:
    encoded = encode_cursor("some-sort-key")
    assert decode_cursor(encoded) == "some-sort-key"


def test_decode_cursor_bad_padding_raises_validation_error() -> None:
    with pytest.raises(ValidationAppError):
        decode_cursor("abc")


def test_decode_cursor_non_utf8_raises_validation_error() -> None:
    with pytest.raises(ValidationAppError):
        decode_cursor("_w==")
