from app.core.i18n import negotiate_locale, translate

SUPPORTED = ["en", "id"]


def test_negotiate_locale_no_header_returns_default() -> None:
    assert negotiate_locale(None, SUPPORTED, "en") == "en"


def test_negotiate_locale_matches_supported_language() -> None:
    assert negotiate_locale("id-ID,en;q=0.8", SUPPORTED, "en") == "id"


def test_negotiate_locale_unsupported_falls_back_to_default() -> None:
    assert negotiate_locale("fr-FR", SUPPORTED, "en") == "en"


def test_negotiate_locale_skips_unparsable_tag() -> None:
    assert negotiate_locale("!!!,id", SUPPORTED, "en") == "id"


def test_translate_english_key() -> None:
    assert translate("user.not_found", "en") == "User not found."


def test_translate_indonesian_key() -> None:
    assert translate("user.not_found", "id") == "Pengguna tidak ditemukan."


def test_translate_unknown_locale_falls_back_to_english() -> None:
    assert translate("user.not_found", "fr") == "User not found."


def test_translate_unknown_key_without_default_returns_key() -> None:
    assert translate("no.such.key", "en") == "no.such.key"


def test_translate_unknown_key_with_default_returns_default() -> None:
    assert translate("no.such.key", "en", default="fallback message") == "fallback message"


def test_translate_interpolates_params() -> None:
    assert translate("role.not_assigned", "en", role="admin") == "You do not have the 'admin' role."
