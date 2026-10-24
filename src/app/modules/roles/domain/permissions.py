"""The catalog of every permission a role can grant.

Defined in code, not in a table: a permission only means something
because a `require_permission(...)` (or an in-handler check like
`files.presentation.router._has_permission`) tests for it, so the code that
checks it is the source of truth and a deploy is what changes it anyway.
`tests/unit/test_permission_catalog.py` fails `make test` if a checked
permission is missing here, or an entry here is checked nowhere.

Pure Python: the domain layer imports no framework.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from app.modules.roles.domain.baseline import WILDCARD_PERMISSION


@dataclass(frozen=True, slots=True)
class PermissionDefinition:
    code: str
    platform_only: bool = False
    """Only platform roles (`developer`/`superadmin`) hold it; never a tenant role."""

    @property
    def resource(self) -> str:
        return self.code.partition(":")[0]

    @property
    def action(self) -> str:
        # `*` has no `:` — it is both the resource and the action.
        return self.code.partition(":")[2] or self.code


def _actions(
    resource: str, *actions: str, platform_only: bool = False
) -> list[PermissionDefinition]:
    return [PermissionDefinition(f"{resource}:{a}", platform_only=platform_only) for a in actions]


PERMISSION_CATALOG: tuple[PermissionDefinition, ...] = (
    PermissionDefinition(WILDCARD_PERMISSION, platform_only=True),
    *_actions("users", "read", "write", "delete"),
    *_actions("roles", "read", "write", "delete"),
    *_actions("audit_log", "read"),
    *_actions("master_examples", "read", "write", "delete"),
    *_actions("examples", "read", "write", "delete"),
    *_actions("posts", "read", "write", "delete"),
    *_actions("files", "read", "write", "delete"),
    *_actions("api_clients", "read", "write"),
    *_actions("tenants", "manage", "delete", platform_only=True),
)

_KNOWN_CODES = frozenset(p.code for p in PERMISSION_CATALOG)


def unknown_permissions(permissions: Iterable[str]) -> list[str]:
    """The entries of `permissions` that aren't in the catalog — they would be
    stored but never grant anything, since no code checks for them."""
    return sorted(set(permissions) - _KNOWN_CODES)
