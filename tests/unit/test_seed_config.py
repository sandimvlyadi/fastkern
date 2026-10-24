"""Seed guards and the baseline-role invariants that tenant isolation rests on."""

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

from app.core.config import Environment
from app.modules.roles.domain.baseline import (
    PLATFORM_ROLE_PERMISSIONS,
    TENANT_ROLE_PERMISSIONS,
    WILDCARD_PERMISSION,
    is_valid_permission,
)

ROOT = Path(__file__).resolve().parents[2]


def _load_seed() -> ModuleType:
    spec = importlib.util.spec_from_file_location("seed_script", ROOT / "scripts" / "seed.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


seed = _load_seed()
GOOD_PASSWORD = "correct-horse-battery-staple"


@pytest.mark.parametrize("environment", [Environment.LOCAL, Environment.TEST])
def test_local_and_test_fall_back_to_dev_defaults(environment: Environment) -> None:
    password, email = seed.resolve_seed_credentials(environment, None, None)
    assert password == seed.DEFAULT_SEED_PASSWORD
    assert email == seed.LOCAL_DEVELOPER_EMAIL


def test_local_honours_explicit_values_and_lowercases_email() -> None:
    password, email = seed.resolve_seed_credentials(
        Environment.LOCAL, "short", "  Dev@Example.COM "
    )
    assert (password, email) == ("short", "dev@example.com")


@pytest.mark.parametrize("environment", [Environment.STAGING, Environment.PRODUCTION])
def test_deployed_environment_accepts_explicit_strong_values(environment: Environment) -> None:
    password, email = seed.resolve_seed_credentials(environment, GOOD_PASSWORD, "Ops@Acme.io")
    assert (password, email) == (GOOD_PASSWORD, "ops@acme.io")


@pytest.mark.parametrize("environment", [Environment.STAGING, Environment.PRODUCTION])
@pytest.mark.parametrize(
    ("password", "email", "fragment"),
    [
        (None, "ops@acme.io", "SEED_DEFAULT_PASSWORD is required"),
        ("", "ops@acme.io", "SEED_DEFAULT_PASSWORD is required"),
        ("ChangePlease123!", "ops@acme.io", "public default"),
        ("short-pw", "ops@acme.io", "at least 12"),
        (GOOD_PASSWORD, None, "SEED_DEVELOPER_EMAIL is required"),
        (GOOD_PASSWORD, "not-an-email", "SEED_DEVELOPER_EMAIL is required"),
    ],
)
def test_deployed_environment_rejects_unsafe_inputs(
    environment: Environment, password: str | None, email: str | None, fragment: str
) -> None:
    with pytest.raises(seed.SeedConfigError, match=fragment):
        seed.resolve_seed_credentials(environment, password, email)


def test_seed_never_hardcodes_a_personal_email_outside_the_local_default() -> None:
    users = seed.build_role_users("someone@acme.io")
    assert users["developer"][1] == "someone@acme.io"
    assert all(
        email.endswith("@example.com") for role, (_, email) in users.items() if role != "developer"
    )


def test_wildcard_only_on_platform_roles() -> None:
    holders = {name for name, perms in seed.ROLE_PERMISSIONS.items() if "*" in perms}
    assert holders == {"developer", "superadmin"} == set(PLATFORM_ROLE_PERMISSIONS)
    assert set(seed.ROLE_PERMISSIONS) == {"developer", "superadmin", "admin", "user"}
    for perms in TENANT_ROLE_PERMISSIONS.values():
        assert WILDCARD_PERMISSION not in perms


def test_tenant_admin_cannot_manage_tenants() -> None:
    # tenants:* is platform-only.
    for perms in seed.ROLE_PERMISSIONS["admin"], seed.ROLE_PERMISSIONS["user"]:
        assert not any(p.startswith("tenants:") for p in perms)


def test_every_baseline_permission_matches_the_permission_format() -> None:
    for perms in (*PLATFORM_ROLE_PERMISSIONS.values(), *TENANT_ROLE_PERMISSIONS.values()):
        assert all(is_valid_permission(p) for p in perms), perms


def test_every_permission_the_code_requires_matches_the_permission_format() -> None:
    # a role can only ever be granted what the format allows, so the format must not
    # exclude a permission the app itself checks.
    found: set[str] = set()
    for path in (ROOT / "src").rglob("*.py"):
        found.update(re.findall(r'require_permission\(\s*"([^"]+)"', path.read_text()))
    assert {"users:read", "tenants:manage", "audit_log:read"} <= found
    assert [p for p in found if not is_valid_permission(p)] == []


@pytest.mark.parametrize("permission", ["*", "users:read", "audit_log:read", "a1:b_2"])
def test_valid_permission_formats(permission: str) -> None:
    assert is_valid_permission(permission)


@pytest.mark.parametrize(
    "permission",
    [
        "",
        "not a perm!!",
        "::",
        "Users:read",
        "users:",
        ":read",
        "users",
        "users:read:x",
        "*:*",
        "users:*",
        "users:read\n",
        "1users:read",
        "users:Read",
    ],
)
def test_invalid_permission_formats(permission: str) -> None:
    assert not is_valid_permission(permission)
