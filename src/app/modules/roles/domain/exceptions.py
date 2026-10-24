from app.core.exceptions import (
    AlreadyExistsError,
    ForbiddenError,
    NotFoundError,
    ValidationAppError,
)


class RoleNotFoundError(NotFoundError):
    code = "role_not_found"
    key = "role.not_found"
    message = "Role not found."


class RoleAlreadyExistsError(AlreadyExistsError):
    code = "role_already_exists"
    key = "role.already_exists"
    message = "A role with this name already exists."


class RoleNotAssignedError(ForbiddenError):
    code = "role_not_assigned"
    key = "role.not_assigned"
    message = "You do not have this role assigned."


class InsufficientPermissionError(ForbiddenError):
    code = "insufficient_permission"
    key = "role.insufficient_permission"
    message = "You do not have permission to perform this action."


class PermissionEscalationError(ForbiddenError):
    code = "permission_escalation"
    key = "role.permission_escalation"
    message = "You cannot grant permissions you do not hold yourself."


class SystemRoleImmutableError(ForbiddenError):
    code = "system_role_immutable"
    key = "role.system_immutable"
    message = "System roles cannot be modified or deleted."


class UnknownPermissionError(ValidationAppError):
    """A role may only be granted permissions from `roles.domain.permissions.PERMISSION_CATALOG`:
    anything else is stored but grants nothing, since no code checks for it."""

    code = "unknown_permission"
    key = "role.unknown_permission"
    message = "These permissions do not exist."
