from dataclasses import dataclass, field
from typing import Any
from uuid import UUID


@dataclass(frozen=True, slots=True)
class CreateRoleInput:
    tenant_id: UUID
    name: str
    description: str = ""
    permissions: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class UpdateRoleInput:
    """Fields left as None are unchanged. `permissions`, when given, replaces
    the whole list (it is not merged)."""

    name: str | None = None
    description: str | None = None
    permissions: list[str] | None = None


@dataclass(frozen=True, slots=True)
class RoleAssignmentChange:
    """A user<->role assignment that a use case actually added or removed,
    returned so the caller can audit it (`None` from the use case means
    nothing changed). `role_name` is None when the role itself is already
    soft-deleted and its name is no longer resolvable."""

    user_id: UUID
    role_id: UUID
    role_name: str | None

    def audit_snapshot(self) -> dict[str, Any]:
        return {
            "userId": str(self.user_id),
            "roleId": str(self.role_id),
            "roleName": self.role_name,
        }


@dataclass(frozen=True, slots=True)
class PermissionCatalogEntry:
    """One catalog permission as seen by a specific caller: `grantable` is whether they could
    put it on a role (or assign a role holding it) without a `PermissionEscalationError`."""

    code: str
    resource: str
    action: str
    platform_only: bool
    grantable: bool
