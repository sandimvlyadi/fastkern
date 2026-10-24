"""Minimal JSON-catalog i18n.

Catalogs live at `app/locales/<locale>.json` as a flat {key: message} map.
Keep it simple on purpose: this is a base project, not a translation
platform — swap in Babel/gettext .po files later if the catalog grows large
without changing the `translate()` call sites.
"""

import json
from functools import lru_cache
from pathlib import Path

from babel import Locale, UnknownLocaleError

_LOCALES_DIR = Path(__file__).resolve().parent.parent / "locales"


@lru_cache
def _load_catalog(locale: str) -> dict[str, str]:
    path = _LOCALES_DIR / f"{locale}.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


@lru_cache(maxsize=256)
def _parse_language_tag(tag: str) -> str | None:
    """The expensive part of locale negotiation (Babel's CLDR-backed
    parse/validate) — cached because real traffic sees a small, bounded
    set of distinct tags (one per client locale) repeated across every
    request, not a fresh tag each time."""
    try:
        return Locale.parse(tag, sep="-").language
    except UnknownLocaleError, ValueError:
        return None


def negotiate_locale(accept_language: str | None, supported: list[str], default: str) -> str:
    if not accept_language:
        return default
    for raw in accept_language.split(","):
        tag = raw.split(";")[0].strip()
        language = _parse_language_tag(tag)
        if language is not None and language in supported:
            return language
    return default


def translate(key: str, locale: str, *, default: str | None = None, **params: object) -> str:
    catalog = _load_catalog(locale)
    template = catalog.get(key) or _load_catalog("en").get(key) or default or key
    return template.format(**params) if params else template
