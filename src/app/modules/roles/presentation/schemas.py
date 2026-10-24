from typing import Annotated
from uuid import UUID

from pydantic import AfterValidator, ConfigDict, Field

from app.core.config import get_settings
from app.modules.roles.domain.baseline import MAX_PERMISSIONS_PER_ROLE, is_valid_permission
from app.shared.application.dto import CamelModel

_DEFAULT_TENANT_ID = get_settings().default_tenant_id


class RoleResponse(CamelModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": "b3f1c9de-8f2a-4a6e-9b1a-6e2d4c8f0a11",
                    "tenantId": _DEFAULT_TENANT_ID,
                    "name": "editor",
                    "description": "Can create and edit content but not manage users.",
                    "permissions": ["users:read", "audit_log:read"],
                    "isSystem": False,
                }
            ]
        }
    )

    id: UUID
    tenant_id: UUID
    name: str
    description: str
    permissions: list[str]
    is_system: bool


class UserRoleResponse(RoleResponse):
    """`RoleResponse` plus whether this is the user's default role — the
    one field `GET /roles/users/{user_id}` adds on top (see router.py)."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": "b3f1c9de-8f2a-4a6e-9b1a-6e2d4c8f0a11",
                    "tenantId": _DEFAULT_TENANT_ID,
                    "name": "editor",
                    "description": "Can create and edit content but not manage users.",
                    "permissions": ["users:read", "audit_log:read"],
                    "isSystem": False,
                    "isDefault": True,
                }
            ]
        }
    )

    is_default: bool


class PermissionResponse(CamelModel):
    """One entry of `GET /roles/permissions` — what a role's `permissions` may contain."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "code": "users:write",
                    "resource": "users",
                    "action": "write",
                    "platformOnly": False,
                    "grantable": True,
                }
            ]
        }
    )

    code: str
    resource: str
    action: str
    platform_only: bool
    grantable: bool


def _validate_permissions(values: list[str]) -> list[str]:
    if len(values) > MAX_PERMISSIONS_PER_ROLE:
        raise ValueError(f"a role can hold at most {MAX_PERMISSIONS_PER_ROLE} permissions")
    if not all(is_valid_permission(value) for value in values):
        # Not echoed back: the offending strings are client input.
        raise ValueError(
            "each permission must be '*' or 'resource:action' "
            "(lowercase letters, digits and underscores)"
        )
    return list(dict.fromkeys(values))  # drop duplicates, keep the caller's order


Permissions = Annotated[list[str], AfterValidator(_validate_permissions)]


class CreateRoleRequest(CamelModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "name": "content-editor",
                    "description": "Can create and edit content.",
                    "permissions": ["users:read", "audit_log:read"],
                }
            ]
        }
    )

    # No tenant_id field: it is always derived server-side from the
    # authenticated caller's request context, never accepted from the
    # client (see roles/presentation/router.py:create_role).
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)  # roles.description is String(500)
    permissions: Permissions = Field(default_factory=list)


class UpdateRoleRequest(CamelModel):
    """Partial update: omitted fields are unchanged. `permissions`, when
    sent, replaces the whole list."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "name": "content-editor",
                    "description": "Can create and edit content.",
                    "permissions": ["users:read", "audit_log:read"],
                }
            ]
        }
    )

    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(
        default=None, max_length=500
    )  # roles.description is String(500)
    permissions: Permissions | None = None


class AssignRoleRequest(CamelModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"roleId": "b3f1c9de-8f2a-4a6e-9b1a-6e2d4c8f0a11"}]}
    )

    role_id: UUID
