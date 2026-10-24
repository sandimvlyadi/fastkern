from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from app.shared.domain.entity import TenantScopedEntity


@dataclass(kw_only=True)
class Role(TenantScopedEntity):
    name: str
    description: str = ""
    permissions: list[str] = field(default_factory=list)
    is_system: bool = False
    """System roles (e.g. 'admin') ship with every tenant and can't be deleted."""

    def has_permission(self, permission: str) -> bool:
        return permission in self.permissions or "*" in self.permissions

    def audit_snapshot(self) -> dict[str, Any]:
        """State recorded as an audit-log `before`/`after` (camelCase, like the API)."""
        return {
            "name": self.name,
            "description": self.description,
            "permissions": list(self.permissions),
            "isSystem": self.is_system,
        }


@dataclass(kw_only=True)
class UserRoleAssignment:
    """A role a user holds, plus whether it is their default — the role
    `active_role` is set to at login/refresh unless the caller has since
    called `/auth/switch-role` (see `auth.application.use_cases`)."""

    role: Role
    is_default: bool


def missing_grants(held_by: Iterable[Role], permissions: Iterable[str]) -> list[str]:
    """The permissions in `permissions` that none of `held_by` grants — what a
    caller would be handing out beyond their own reach. A `*` holder can grant
    anything; anyone else must hold each permission (including `*` itself)
    literally, since `has_permission`'s wildcard is not transferable."""
    roles = list(held_by)
    if any("*" in role.permissions for role in roles):
        return []
    granted = {permission for role in roles for permission in role.permissions}
    return sorted(set(permissions) - granted)
