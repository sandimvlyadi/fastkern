"""Structural guard: the permission and scope catalogs match what the code actually checks.

Same idea as `test_rate_limit_coverage.py` — a new endpoint guarded by a permission (or scope)
that nobody added to the catalog would be ungrantable through the API (create/update reject
anything outside the catalog), and a catalog entry nothing checks would grant nothing. Either
fails `make test` here instead of shipping.
"""

import inspect
import re
from pathlib import Path
from typing import Any

from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, iter_route_contexts

from app.main import app
from app.modules.api_clients.domain.scopes import API_KEY_SCOPE_CATALOG, is_valid_scope
from app.modules.roles.domain.baseline import (
    PLATFORM_ROLE_PERMISSIONS,
    TENANT_ROLE_PERMISSIONS,
    WILDCARD_PERMISSION,
    is_valid_permission,
)
from app.modules.roles.domain.permissions import PERMISSION_CATALOG, unknown_permissions

ROOT = Path(__file__).resolve().parents[2]

# Permissions checked inside a handler rather than by a route dependency (so route
# introspection can't see them): `files` widens a caller's own-files view to all files.
_IN_HANDLER_CHECK = re.compile(r'_has_permission\(\s*\w+\s*,\s*"([^"]+)"')


def _calls(dependant: Dependant) -> list[Any]:
    calls: list[Any] = []
    for sub in dependant.dependencies:
        calls += _calls(sub)
        calls.append(sub.call)
    return calls


def _checked(factory: str, argument: str) -> set[str]:
    """Every string a route passes to `require_permission(...)`/`require_scope(...)`, read off
    the dependency closures of the routes the app actually serves (not from source text, which
    would also pick up docstring examples)."""
    found: set[str] = set()
    for rc in iter_route_contexts(app.routes):
        if not isinstance(rc.original_route, APIRoute):
            continue
        for call in _calls(rc.dependant):
            if getattr(call, "__qualname__", "").startswith(f"{factory}."):
                found.add(inspect.getclosurevars(call).nonlocals[argument])
    return found


def _checked_permissions() -> set[str]:
    found = _checked("require_permission", "permission")
    for path in (ROOT / "src" / "app").rglob("*.py"):
        found.update(_IN_HANDLER_CHECK.findall(path.read_text()))
    return found


def test_introspection_finds_the_checks_it_should() -> None:
    # Guards the checks below against silently comparing against nothing.
    assert {"users:read", "tenants:manage", "files:read"} <= _checked_permissions()
    assert {"posts:read", "users:read", "files:read"} <= _checked("require_scope", "scope")


def test_every_checked_permission_is_in_the_catalog() -> None:
    assert unknown_permissions(_checked_permissions()) == []


def test_every_catalog_permission_is_checked_somewhere() -> None:
    catalog = {p.code for p in PERMISSION_CATALOG} - {WILDCARD_PERMISSION}
    assert sorted(catalog - _checked_permissions()) == []


def test_every_checked_scope_is_in_the_catalog_and_vice_versa() -> None:
    assert {s.code for s in API_KEY_SCOPE_CATALOG} == _checked("require_scope", "scope")


def test_catalog_codes_are_unique_and_well_formed() -> None:
    codes = [p.code for p in PERMISSION_CATALOG]
    assert len(codes) == len(set(codes))
    assert all(is_valid_permission(code) for code in codes)
    scopes = [s.code for s in API_KEY_SCOPE_CATALOG]
    assert len(scopes) == len(set(scopes))
    assert all(is_valid_scope(scope) for scope in scopes)


def test_baseline_roles_only_use_catalog_permissions() -> None:
    for perms in (*PLATFORM_ROLE_PERMISSIONS.values(), *TENANT_ROLE_PERMISSIONS.values()):
        assert unknown_permissions(perms) == []


def test_platform_only_entries_are_exactly_the_wildcard_and_tenants() -> None:
    platform_only = {p.code for p in PERMISSION_CATALOG if p.platform_only}
    assert platform_only == {WILDCARD_PERMISSION, "tenants:manage", "tenants:delete"}
    # A tenant role must never be provisioned with something only platform roles may hold.
    for perms in TENANT_ROLE_PERMISSIONS.values():
        assert not platform_only & set(perms)


def test_resource_and_action_are_split_from_the_code() -> None:
    by_code = {p.code: p for p in PERMISSION_CATALOG}
    assert (by_code["audit_log:read"].resource, by_code["audit_log:read"].action) == (
        "audit_log",
        "read",
    )
    assert (by_code["*"].resource, by_code["*"].action) == ("*", "*")
