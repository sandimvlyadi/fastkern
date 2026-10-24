"""Every wire schema documents a realistic example.

Same enforcement pattern as `test_i18n_key_coverage.py`: the schemas are discovered, so a new
module's `presentation/schemas.py` is covered without touching this test.
"""

import importlib
import inspect
from pathlib import Path

import pytest

from app.shared.application.dto import CamelModel

MODULES_DIR = Path(__file__).resolve().parents[2] / "src" / "app" / "modules"


def _schema_classes() -> list[type[CamelModel]]:
    found: list[type[CamelModel]] = []
    for path in sorted(MODULES_DIR.glob("*/presentation/schemas.py")):
        module = importlib.import_module(
            f"app.modules.{path.parent.parent.name}.presentation.schemas"
        )
        found.extend(
            cls
            for _, cls in inspect.getmembers(module, inspect.isclass)
            if issubclass(cls, CamelModel) and cls.__module__ == module.__name__
        )
    return found


def test_schema_discovery_finds_the_known_schemas() -> None:
    names = {cls.__name__ for cls in _schema_classes()}
    assert {"RegisterRequest", "UserResponse", "CreateRoleRequest", "CreateTenantRequest"} <= names


@pytest.mark.parametrize("schema", _schema_classes(), ids=lambda cls: cls.__name__)
def test_schema_has_an_example(schema: type[CamelModel]) -> None:
    examples = schema.model_json_schema().get("examples")
    assert examples, (
        f"{schema.__module__}.{schema.__name__} has no example: add "
        "`model_config = ConfigDict(json_schema_extra={'examples': [...]})` with one "
        "realistic value (use a placeholder like '<refresh-token-returned-by-login>', never a "
        "JWT-shaped string, or gitleaks will fail the commit)."
    )
    assert all(isinstance(example, dict) and example for example in examples)


@pytest.mark.parametrize("schema", _schema_classes(), ids=lambda cls: cls.__name__)
def test_schema_example_does_not_look_like_a_secret(schema: type[CamelModel]) -> None:
    text = str(schema.model_json_schema().get("examples"))
    assert "eyJ" not in text, "JWT-shaped example values trip gitleaks; use an obvious placeholder"
