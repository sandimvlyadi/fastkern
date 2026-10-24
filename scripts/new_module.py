#!/usr/bin/env python3
"""Scaffold a new module's four-layer skeleton and register it with
import-linter, so every module starts with an identical, enforced shape.

Usage: python scripts/new_module.py <module_name>
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MODULES_DIR = REPO_ROOT / "src" / "app" / "modules"
PYPROJECT = REPO_ROOT / "pyproject.toml"

LAYERS = ("domain", "application", "infrastructure", "presentation")


def validate_name(name: str) -> None:
    if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        print(f"Invalid module name '{name}': use lower_snake_case.", file=sys.stderr)
        sys.exit(1)
    if (MODULES_DIR / name).exists():
        print(f"Module '{name}' already exists at {MODULES_DIR / name}", file=sys.stderr)
        sys.exit(1)


def scaffold(name: str) -> None:
    module_dir = MODULES_DIR / name
    (module_dir / "__init__.py").parent.mkdir(parents=True, exist_ok=True)
    (module_dir / "__init__.py").write_text("")

    for layer in LAYERS:
        layer_dir = module_dir / layer
        layer_dir.mkdir(exist_ok=True)
        (layer_dir / "__init__.py").write_text("")

    (module_dir / "domain" / "entities.py").write_text(
        '"""Domain entities for the `{name}` module. Plain dataclasses only —\n'
        'no FastAPI/SQLAlchemy imports (enforced by import-linter).\n"""\n\n'
        "from dataclasses import dataclass\n\n"
        "from app.shared.domain.entity import TenantScopedEntity\n\n\n"
        "# @dataclass(kw_only=True)\n"
        "# class {Name}(TenantScopedEntity):\n"
        "#     name: str\n".format(name=name, Name=name.title().replace("_", ""))
    )
    (module_dir / "domain" / "exceptions.py").write_text(
        "from app.core.exceptions import AlreadyExistsError, NotFoundError\n\n\n"
        f"class {name.title().replace('_', '')}NotFoundError(NotFoundError):\n"
        f'    code = "{name}_not_found"\n'
        f'    key = "{name}.not_found"\n'
        f'    message = "{name.title()} not found."\n'
    )
    (module_dir / "domain" / "ports.py").write_text(
        "from abc import ABC, abstractmethod\n\n\n"
        "# class {Name}RepositoryPort(ABC):\n"
        "#     @abstractmethod\n"
        "#     async def get_by_id(self, entity_id): ...\n".format(
            Name=name.title().replace("_", "")
        )
    )
    (module_dir / "application" / "use_cases.py").write_text(
        f'"""Use cases for the `{name}` module — one class per use case."""\n'
    )
    (module_dir / "infrastructure" / "models.py").write_text(
        "from app.shared.infrastructure.orm_base import StandardModel\n\n\n"
        "# class {Name}Model(StandardModel):\n"
        "#     # Prefix is mandatory: sys_ (platform), master_ (reference data) or\n"
        "#     # trans_ (business transactions). Every table and column needs a\n"
        "#     # comment=; see docs/module-guide.md.\n"
        '#     __tablename__ = "trans_{name}"\n'
        '#     __table_args__ = ({{"comment": "What this table holds."}},)\n'.format(
            Name=name.title().replace("_", ""), name=name
        )
    )
    (module_dir / "infrastructure" / "repository.py").write_text(
        "from app.shared.infrastructure.repository import BaseRepository\n\n\n"
        "# class {Name}Repository(BaseRepository):\n"
        "#     model = {Name}Model\n".format(Name=name.title().replace("_", ""))
    )
    (module_dir / "presentation" / "schemas.py").write_text(
        "from app.shared.application.dto import CamelModel\n\n\n"
        "# class {Name}Response(CamelModel):\n"
        "#     id: str\n".format(Name=name.title().replace("_", ""))
    )
    url_slug = name.replace("_", "-")
    (module_dir / "presentation" / "router.py").write_text(
        "from fastapi import APIRouter, Depends\n\n"
        "from app.core.di import get_current_user\n"
        "from app.core.rate_limit import RateLimitTier, rate_limit\n\n"
        f'router = APIRouter(\n    prefix="/{url_slug}",\n    tags=["{url_slug}"],\n'
        "    dependencies=[\n"
        "        Depends(rate_limit(RateLimitTier.IP)),  # first: counts requests auth rejects\n"
        "        Depends(get_current_user),\n"
        "        Depends(rate_limit(RateLimitTier.DEFAULT)),\n"
        "        # Depends(require_permission(...)) goes AFTER the default limiter.\n"
        "    ],\n"
        ")\n"
        "\n"
        "# Mutating endpoints also add\n"
        "#     dependencies=[Depends(rate_limit(RateLimitTier.WRITE))],\n"
        "# and every endpoint lists RateLimitedError in its error_responses(...).\n"
    )

    print(f"Created src/app/modules/{name}/ with the four Clean Architecture layers.")


# Matches an existing "<mod> must not import other modules' infrastructure" forbidden
# contract block, up through the end of its `forbidden_modules = [...]` list. Used both to
# find every module already covered (so the new module's own block forbids all of them) and
# to append the new module's infrastructure to each of theirs.
_FORBIDDEN_CONTRACT = re.compile(
    r"name = \"(?P<mod>[a-z_]+) must not import other modules' infrastructure\"\n"
    r'type = "forbidden"\n'
    r"source_modules = \[[^\]]*?\]\n"
    r"forbidden_modules = \[\n"
    r"(?P<forbidden>(?:    \"app\.modules\.[a-z_]+\.infrastructure\",\n)+)\]\n"
)


def register_with_import_linter(name: str) -> None:
    text = PYPROJECT.read_text()
    marker = f'    "app.modules.{name}",\n'
    if marker in text:
        return  # already fully registered by a previous run; no-op

    updated = re.sub(
        r'(containers = \[\n(?:    "app\.modules\.[a-z_]+",\n)+)(\])',
        r"\1" + marker + r"\2",
        text,
    )
    if updated == text:
        print(
            "Could not auto-register the module with import-linter — "
            "add it to the `containers`/`source_modules`/`forbidden_modules` "
            "lists in pyproject.toml's [tool.importlinter] section manually.",
            file=sys.stderr,
        )
        return
    text = updated

    # Every module already covered by this contract pattern (before adding the new one's own
    # block) is exactly the set whose forbidden_modules list must also learn about `name`.
    existing_mods = [m.group("mod") for m in _FORBIDDEN_CONTRACT.finditer(text)]

    def _append_forbidden(match: re.Match[str]) -> str:
        if match.group("mod") == name:
            return match.group(0)
        # Strip the trailing "]\n" and re-add it after one more forbidden_modules entry.
        return match.group(0)[:-2] + f'    "app.modules.{name}.infrastructure",\n]\n'

    text = _FORBIDDEN_CONTRACT.sub(_append_forbidden, text)

    new_block = (
        "\n[[tool.importlinter.contracts]]\n"
        f'name = "{name} must not import other modules\' infrastructure"\n'
        'type = "forbidden"\n'
        f'source_modules = ["app.modules.{name}.domain", "app.modules.{name}.application", '
        f'"app.modules.{name}.presentation"]\n'
        "forbidden_modules = [\n"
        + "".join(f'    "app.modules.{mod}.infrastructure",\n' for mod in existing_mods)
        + "]\n"
        'ignore_imports = ["app.core.di -> app.modules.*.infrastructure.*"]\n'
    )
    PYPROJECT.write_text(text.rstrip("\n") + "\n" + new_block)
    print(
        "Registered the module in pyproject.toml's import-linter `containers` list, added its "
        "own 'must not import other modules' infrastructure' contract, and added it to every "
        "existing module's forbidden_modules list."
    )


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    name = sys.argv[1]
    validate_name(name)
    scaffold(name)
    register_with_import_linter(name)
    print("\nNext: see docs/module-guide.md, then register the router in src/app/main.py.")


if __name__ == "__main__":
    main()
