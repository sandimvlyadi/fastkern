import pytest
from pydantic import ValidationError

from app.modules.users.presentation.schemas import UpdateUserRequest


@pytest.mark.parametrize("locale", ["en", "id", None])
def test_supported_locale_is_accepted(locale: str | None) -> None:
    assert UpdateUserRequest(locale=locale).locale == locale


@pytest.mark.parametrize("locale", ["xx", "en-US", "EN", "", "id "])
def test_unsupported_locale_is_rejected_without_echoing_it(locale: str) -> None:
    with pytest.raises(ValidationError) as exc:
        UpdateUserRequest(locale=locale)
    assert "locale must be one of: en, id" in str(exc.value)


@pytest.mark.parametrize("timezone", ["UTC", "Asia/Jakarta", "America/New_York", None])
def test_known_timezone_is_accepted(timezone: str | None) -> None:
    assert UpdateUserRequest(timezone=timezone).timezone == timezone


@pytest.mark.parametrize(
    "timezone", ["Mars/Olympus", "asia/jakarta", "", "+07:00", "../etc/passwd"]
)
def test_unknown_timezone_is_rejected(timezone: str) -> None:
    with pytest.raises(ValidationError, match="valid IANA timezone"):
        UpdateUserRequest(timezone=timezone)


def test_camel_case_payload_still_parses() -> None:
    parsed = UpdateUserRequest.model_validate(
        {"fullName": "Jane", "locale": "id", "timezone": "Asia/Jakarta"}
    )
    assert (parsed.full_name, parsed.locale, parsed.timezone) == ("Jane", "id", "Asia/Jakarta")
