"""Baseline role definitions and the permission-string format.

Single source of truth shared by the seed script (which creates the platform
roles and a first tenant) and tenant provisioning (which gives every new tenant
the tenant-level roles), so the two can never drift apart. Pure Python: the domain
layer imports no framework.
"""

import re

WILDCARD_PERMISSION = "*"

# Platform-level roles: the only ones allowed to hold `*` (which also covers the platform-only
# `tenants:*` permissions). Never provisioned into a tenant automatically — the seed script
# creates them for the bootstrap tenant.
PLATFORM_ROLE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "developer": (WILDCARD_PERMISSION,),
    "superadmin": (WILDCARD_PERMISSION,),
}

# Tenant-level system roles: every tenant gets these. Deliberately no `*` and no `tenants:*`
# (those stay platform-only): a tenant admin must not manage other tenants.
TENANT_ROLE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "admin": (
        "users:read",
        "users:write",
        "users:delete",
        "roles:read",
        "roles:write",
        "roles:delete",
        "audit_log:read",
        "master_examples:read",
        "master_examples:write",
        "master_examples:delete",
        "examples:read",
        "examples:write",
        "examples:delete",
        "posts:read",
        "posts:write",
        "posts:delete",
        "files:read",
        "files:write",
        "files:delete",
        "api_clients:read",
        "api_clients:write",
    ),
    "user": (),
}

# `*` or `resource:action` — lowercase snake_case segments only. No `resource:*`:
# `Role.has_permission` only honors a literal match or the bare `*`, so a per-resource
# wildcard would be stored but never grant anything.
PERMISSION_PATTERN = re.compile(r"^(\*|[a-z][a-z0-9_]*:[a-z][a-z0-9_]*)$")
MAX_PERMISSIONS_PER_ROLE = 100


def is_valid_permission(permission: str) -> bool:
    return PERMISSION_PATTERN.fullmatch(permission) is not None
