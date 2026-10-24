from app.core.exceptions import AlreadyExistsError, ForbiddenError, NotFoundError


class TenantNotFoundError(NotFoundError):
    code = "tenant_not_found"
    key = "tenant.not_found"
    message = "Tenant not found."


class TenantSlugAlreadyExistsError(AlreadyExistsError):
    code = "tenant_slug_already_exists"
    key = "tenant.slug_already_exists"
    message = "A tenant with this slug already exists."


class TenantInactiveError(ForbiddenError):
    code = "tenant_inactive"
    key = "tenant.inactive"
    message = "This tenant is not active."


class CannotModifyOwnTenantError(ForbiddenError):
    code = "cannot_modify_own_tenant"
    key = "tenant.cannot_modify_own"
    message = "You cannot deactivate or delete your own tenant."
