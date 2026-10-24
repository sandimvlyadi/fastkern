from collections.abc import Iterable
from dataclasses import replace
from uuid import UUID

from app.core.pagination import OffsetPage, OffsetParams, paginate_sequence
from app.modules.roles.application.dto import (
    CreateRoleInput,
    PermissionCatalogEntry,
    RoleAssignmentChange,
    UpdateRoleInput,
)
from app.modules.roles.domain.baseline import TENANT_ROLE_PERMISSIONS
from app.modules.roles.domain.entities import Role, UserRoleAssignment, missing_grants
from app.modules.roles.domain.exceptions import (
    PermissionEscalationError,
    RoleAlreadyExistsError,
    RoleNotAssignedError,
    RoleNotFoundError,
    SystemRoleImmutableError,
    UnknownPermissionError,
)
from app.modules.roles.domain.permissions import PERMISSION_CATALOG, unknown_permissions
from app.modules.roles.domain.ports import RoleRepositoryPort, UserRoleRepositoryPort
from app.modules.users.domain.exceptions import UserNotFoundError
from app.modules.users.domain.ports import UserRepositoryPort


async def _ensure_can_grant(
    user_roles: UserRoleRepositoryPort, caller_id: UUID, permissions: Iterable[str]
) -> None:
    """A caller with `roles:write` may only hand out permissions they hold
    themselves (across all their assigned roles, like `require_permission`).
    Without this, `roles:write` alone is a path to `*`: create or edit a role
    with any permission and assign it to yourself, or assign an existing
    `*` role such as `superadmin`."""
    missing = missing_grants(await user_roles.list_roles_for_user(caller_id), permissions)
    if missing:
        raise PermissionEscalationError(permissions=", ".join(missing))


def _ensure_known(permissions: Iterable[str]) -> None:
    """Checked before `_ensure_can_grant`, so even a `*` caller can't store a permission no
    code checks for (a typo such as `user:read` would otherwise silently grant nothing)."""
    unknown = unknown_permissions(permissions)
    if unknown:
        raise UnknownPermissionError(permissions=", ".join(unknown))


class CreateRoleUseCase:
    def __init__(self, roles: RoleRepositoryPort, user_roles: UserRoleRepositoryPort) -> None:
        self._roles = roles
        self._user_roles = user_roles

    async def execute(self, data: CreateRoleInput, caller_id: UUID) -> Role:
        if await self._roles.get_by_name(data.name) is not None:
            raise RoleAlreadyExistsError(name=data.name)
        _ensure_known(data.permissions)
        await _ensure_can_grant(self._user_roles, caller_id, data.permissions)
        role = Role(
            tenant_id=data.tenant_id,
            name=data.name,
            description=data.description,
            permissions=data.permissions,
        )
        return await self._roles.add(role)


class UpdateRoleUseCase:
    def __init__(self, roles: RoleRepositoryPort, user_roles: UserRoleRepositoryPort) -> None:
        self._roles = roles
        self._user_roles = user_roles

    async def execute(
        self, role_id: UUID, data: UpdateRoleInput, caller_id: UUID
    ) -> tuple[Role, Role]:
        """Returns (before, after) for the caller's audit-log diff. System
        roles are refused: `require_role(...)` checks and the seed script
        look them up by name, so renaming or stripping one would break them.
        Only permissions being *added* must exist in the catalog and be ones
        the caller holds — keeping or dropping an existing one never widens
        anyone's access, and a stale entry must not block an unrelated edit."""
        role = await self._roles.get_by_id(role_id)
        if role is None:
            raise RoleNotFoundError(role_id=str(role_id))
        if role.is_system:
            raise SystemRoleImmutableError(role=role.name)
        before = replace(role)
        if data.name is not None and data.name != role.name:
            existing = await self._roles.get_by_name(data.name)
            if existing is not None and existing.id != role.id:
                raise RoleAlreadyExistsError(name=data.name)
            role.name = data.name
        if data.description is not None:
            role.description = data.description
        if data.permissions is not None:
            added = set(data.permissions) - set(role.permissions)
            _ensure_known(added)
            await _ensure_can_grant(self._user_roles, caller_id, added)
            # Rebound, never mutated in place: `before` shares the old list.
            role.permissions = list(data.permissions)
        after = await self._roles.save(role)
        return before, after


class DeleteRoleUseCase:
    def __init__(self, roles: RoleRepositoryPort) -> None:
        self._roles = roles

    async def execute(self, role_id: UUID) -> Role:
        """Soft-deletes a user-created role and returns it as it was, for the audit log."""
        role = await self._roles.get_by_id(role_id)
        if role is None:
            raise RoleNotFoundError(role_id=str(role_id))
        if role.is_system:
            raise SystemRoleImmutableError(role=role.name)
        before = replace(role)
        await self._roles.delete(role_id)
        return before


_BASELINE_ROLE_DESCRIPTIONS = {
    "admin": "Tenant administrator: manages users and roles, reads the audit log.",
    "user": "Standard member without extra permissions.",
}


class ProvisionBaselineRolesUseCase:
    """Creates the tenant-level system roles for a newly created tenant. The definitions are
    the same ones the seed script uses (`roles.domain.baseline`), so a seeded tenant and a
    freshly created one start out identical. Idempotent: a role that already exists is kept."""

    def __init__(self, roles: RoleRepositoryPort) -> None:
        self._roles = roles

    async def execute(self, tenant_id: UUID) -> list[Role]:
        created: list[Role] = []
        for name, permissions in TENANT_ROLE_PERMISSIONS.items():
            if await self._roles.get_by_name(name, tenant_id=tenant_id) is not None:
                continue
            role = Role(
                tenant_id=tenant_id,
                name=name,
                description=_BASELINE_ROLE_DESCRIPTIONS.get(name, ""),
                permissions=list(permissions),
                is_system=True,
            )
            created.append(await self._roles.add(role))
        return created


class ListRolesUseCase:
    def __init__(self, roles: RoleRepositoryPort) -> None:
        self._roles = roles

    async def execute(self, params: OffsetParams) -> OffsetPage[Role]:
        return await self._roles.paginate(params)


class ListPermissionCatalogUseCase:
    """The permissions a role can be given, flagged with whether `caller_id` may grant each
    one (the same `missing_grants` rule `_ensure_can_grant` enforces), so a client can grey
    out what a create/update would refuse. Platform-only entries (`*`, `tenants:*`) are
    listed only to a caller who can grant them: a tenant admin never sees them."""

    def __init__(self, user_roles: UserRoleRepositoryPort) -> None:
        self._user_roles = user_roles

    async def execute(
        self, caller_id: UUID, params: OffsetParams
    ) -> OffsetPage[PermissionCatalogEntry]:
        held = await self._user_roles.list_roles_for_user(caller_id)
        entries: list[PermissionCatalogEntry] = []
        for permission in PERMISSION_CATALOG:
            grantable = not missing_grants(held, [permission.code])
            if permission.platform_only and not grantable:
                continue
            entries.append(
                PermissionCatalogEntry(
                    code=permission.code,
                    resource=permission.resource,
                    action=permission.action,
                    platform_only=permission.platform_only,
                    grantable=grantable,
                )
            )
        return paginate_sequence(entries, params)


class AssignRoleToUserUseCase:
    def __init__(
        self,
        roles: RoleRepositoryPort,
        user_roles: UserRoleRepositoryPort,
        users: UserRepositoryPort,
    ) -> None:
        self._roles = roles
        self._user_roles = user_roles
        self._users = users

    async def execute(
        self, user_id: UUID, role_id: UUID, caller_id: UUID
    ) -> RoleAssignmentChange | None:
        """Returns the new assignment, or None when the user already had the
        role (assignment is idempotent) — callers audit only an actual change.
        The caller must hold every permission the role grants. A user's very
        first role assignment automatically becomes their default."""
        role = await self._roles.get_by_id(role_id)
        if role is None:
            raise RoleNotFoundError(role_id=str(role_id))
        await _ensure_can_grant(self._user_roles, caller_id, role.permissions)
        # get_by_id is tenant-scoped by default (ambient request context),
        # so this also rejects assigning a role to a user outside the
        # caller's own tenant instead of silently succeeding cross-tenant.
        if await self._users.get_by_id(user_id) is None:
            raise UserNotFoundError(user_id=str(user_id))
        existing = await self._user_roles.list_roles_for_user(user_id)
        is_default = len(existing) == 0
        if not await self._user_roles.assign(user_id, role_id, is_default=is_default):
            return None
        return RoleAssignmentChange(user_id=user_id, role_id=role_id, role_name=role.name)


class RevokeRoleFromUserUseCase:
    def __init__(self, roles: RoleRepositoryPort, user_roles: UserRoleRepositoryPort) -> None:
        self._roles = roles
        self._user_roles = user_roles

    async def execute(self, user_id: UUID, role_id: UUID) -> RoleAssignmentChange | None:
        """Returns the removed assignment, or None when the user never had
        the role — callers audit only an actual change. The assignment is
        removed even if the role was soft-deleted meanwhile (its name is then
        unresolvable, so `role_name` is None) — otherwise a dangling
        assignment could never be cleaned up through the API. If the revoked
        role was the user's default and others remain, the next one
        (alphabetically) is promoted so the user always has a default while
        they hold any role."""
        role = await self._roles.get_by_id(role_id)
        if not await self._user_roles.revoke(user_id, role_id):
            return None
        # Cheap check first: the common case (a non-default role was revoked) stops here
        # with one extra query instead of two.
        if await self._user_roles.get_default_role_for_user(user_id) is None:
            remaining = await self._user_roles.list_roles_for_user(user_id)
            if remaining:
                await self._user_roles.set_default_role(user_id, remaining[0].id)
        return RoleAssignmentChange(
            user_id=user_id, role_id=role_id, role_name=role.name if role else None
        )


class ListUserRolesUseCase:
    def __init__(self, user_roles: UserRoleRepositoryPort) -> None:
        self._user_roles = user_roles

    async def execute(self, user_id: UUID, params: OffsetParams) -> OffsetPage[UserRoleAssignment]:
        return await self._user_roles.paginate_roles_for_user(user_id, params)


class SetDefaultRoleUseCase:
    """Marks one of a user's already-assigned roles as their default (used
    as `active_role` at login/refresh unless they've since switched)."""

    def __init__(
        self,
        roles: RoleRepositoryPort,
        user_roles: UserRoleRepositoryPort,
        users: UserRepositoryPort,
    ) -> None:
        self._roles = roles
        self._user_roles = user_roles
        self._users = users

    async def execute(self, user_id: UUID, role_id: UUID) -> Role:
        role = await self._roles.get_by_id(role_id)
        if role is None:
            raise RoleNotFoundError(role_id=str(role_id))
        # An unknown user is a 404, as in AssignRoleToUserUseCase, not RoleNotAssignedError.
        if await self._users.get_by_id(user_id) is None:
            raise UserNotFoundError(user_id=str(user_id))
        if not await self._user_roles.set_default_role(user_id, role_id):
            raise RoleNotAssignedError(role=role.name)
        return role


class SwitchActiveRoleUseCase:
    """Validates that the requested role is one of the user's assigned
    roles. Reissuing the JWT with the new `active_role` claim is done by
    the auth module, which calls this use case first."""

    def __init__(self, user_roles: UserRoleRepositoryPort) -> None:
        self._user_roles = user_roles

    async def execute(self, user_id: UUID, role_name: str) -> Role:
        roles = await self._user_roles.list_roles_for_user(user_id)
        for role in roles:
            if role.name == role_name:
                return role
        raise RoleNotAssignedError(role=role_name)
