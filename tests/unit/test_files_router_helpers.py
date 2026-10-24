"""Pure-function helpers in the files router, tested directly — no HTTP client needed."""

from app.core.config import get_settings
from app.modules.files.infrastructure.storage.local_fs import LocalFileSystemStorage
from app.modules.files.presentation.router import _parse_content_length, get_object_storage


def test_get_object_storage_builds_a_real_adapter_from_settings() -> None:
    storage = get_object_storage(get_settings())
    assert isinstance(storage, LocalFileSystemStorage)


def test_parse_content_length_missing_header_is_none() -> None:
    assert _parse_content_length(None) is None


def test_parse_content_length_valid_header_is_parsed() -> None:
    assert _parse_content_length("1024") == 1024


def test_parse_content_length_malformed_header_is_none() -> None:
    """Content-Length is only a fast-reject hint; a malformed value is ignored rather than
    rejecting the request outright — the streamed byte count is the real guard."""
    assert _parse_content_length("not-a-number") is None
