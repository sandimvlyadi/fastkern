"""Guardrail for "every user-facing string is translated": fails the suite if a new
`AppError` subclass is added without its own translation `key`, or if a `key` in use has no
matching entry in one of the locale catalogs.
"""

import importlib
import json
from pathlib import Path

from app.core.exceptions import AppError

_LOCALES_DIR = Path(__file__).resolve().parents[2] / "src" / "app" / "locales"
_MODULES_DIR = Path(__file__).resolve().parents[2] / "src" / "app" / "modules"

# Import every module's domain/exceptions.py so their AppError subclasses are registered
# before we walk __subclasses__() below. Discovered from disk (like test_schema_standards.py
# discovers models.py) so a new module is covered automatically, without editing this file.
for _exc_file in sorted(_MODULES_DIR.glob("*/domain/exceptions.py")):
    importlib.import_module(f"app.modules.{_exc_file.parent.parent.name}.domain.exceptions")


def _all_app_error_subclasses() -> list[type[AppError]]:
    """Every AppError subclass defined in production code (`app.*`) —
    excludes ad-hoc subclasses test modules define for their own fixtures.
    """
    seen: set[type[AppError]] = set()
    stack = [AppError]
    result: list[type[AppError]] = []
    while stack:
        cls = stack.pop()
        for subclass in cls.__subclasses__():
            if subclass in seen:
                continue
            seen.add(subclass)
            stack.append(subclass)
            if subclass.__module__.startswith("app."):
                result.append(subclass)
    return result


def _load_catalog(locale: str) -> dict[str, str]:
    return json.loads((_LOCALES_DIR / f"{locale}.json").read_text(encoding="utf-8"))


def test_every_production_app_error_subclass_declares_its_own_key() -> None:
    """Inheriting AppError's generic `key = "error.internal"` silently is
    almost never what a new exception class wants — every subclass must
    set its own `key` explicitly, the same way every subclass already
    sets its own `code`."""
    subclasses = _all_app_error_subclasses()
    assert subclasses, "expected at least one AppError subclass to be importable"

    missing = [cls for cls in subclasses if "key" not in cls.__dict__]
    assert not missing, (
        "these AppError subclasses inherit `key` instead of declaring their own "
        f'(add `key = "<module>.<reason>"` + matching en.json/id.json entries): '
        f"{[cls.__qualname__ for cls in missing]}"
    )


def test_every_app_error_key_exists_in_every_locale_catalog() -> None:
    keys = {AppError.key} | {cls.key for cls in _all_app_error_subclasses()}
    en = _load_catalog("en")
    id_ = _load_catalog("id")

    missing_en = sorted(keys - en.keys())
    missing_id = sorted(keys - id_.keys())
    assert not missing_en, f"keys missing from src/app/locales/en.json: {missing_en}"
    assert not missing_id, f"keys missing from src/app/locales/id.json: {missing_id}"


def test_locale_catalogs_have_no_orphan_or_mismatched_keys() -> None:
    """en.json and id.json must carry exactly the same key set — a key
    added to one without the other silently breaks translation for
    whichever locale is missing it (translate() falls back to English,
    masking the gap instead of failing loudly)."""
    en_keys = _load_catalog("en").keys()
    id_keys = _load_catalog("id").keys()
    assert en_keys == id_keys, (
        f"en.json/id.json key sets differ — only in en.json: {sorted(en_keys - id_keys)}, "
        f"only in id.json: {sorted(id_keys - en_keys)}"
    )
