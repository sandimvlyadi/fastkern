from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import get_request_context
from app.core.db import get_db_session
from app.core.di import (
    get_audit_log_repository,
    get_current_user,
    get_user_repository,
    record_audit_event,
    require_permission,
)
from app.core.errors import error_responses
from app.core.exceptions import RateLimitedError
from app.core.pagination import OffsetParams
from app.core.rate_limit import RateLimitTier, rate_limit
from app.core.response import SuccessEnvelope, success_envelope
from app.modules.audit_log.domain.ports import AuditLogRepositoryPort
from app.modules.auth.domain.exceptions import InvalidTokenError
from app.modules.roles.application.dto import CreateRoleInput, UpdateRoleInput
from app.modules.roles.application.use_cases import (
    AssignRoleToUserUseCase,
    CreateRoleUseCase,
    DeleteRoleUseCase,
    ListPermissionCatalogUseCase,
    ListRolesUseCase,
    ListUserRolesUseCase,
    RevokeRoleFromUserUseCase,
    SetDefaultRoleUseCase,
    UpdateRoleUseCase,
)
from app.modules.roles.domain.exceptions import (
    InsufficientPermissionError,
    PermissionEscalationError,
    RoleAlreadyExistsError,
    RoleNotAssignedError,
    RoleNotFoundError,
    SystemRoleImmutableError,
    UnknownPermissionError,
)
from app.modules.roles.infrastructure.repository import RoleRepository, UserRoleRepository
from app.modules.roles.presentation.schemas import (
    AssignRoleRequest,
    CreateRoleRequest,
    PermissionResponse,
    RoleResponse,
    UpdateRoleRequest,
    UserRoleResponse,
)
from app.modules.users.domain.exceptions import UserNotFoundError
from app.modules.users.domain.ports import UserRepositoryPort

router = APIRouter(
    prefix="/roles",
    tags=["roles"],
    dependencies=[
        Depends(rate_limit(RateLimitTier.IP)),
        Depends(get_current_user),
        Depends(rate_limit(RateLimitTier.DEFAULT)),
    ],
)


@router.get(
    "",
    response_model=SuccessEnvelope[list[RoleResponse]],
    responses=error_responses(
        InvalidTokenError, InsufficientPermissionError, RateLimitedError, validation=True
    ),
)
async def list_roles(
    params: OffsetParams = Depends(),
    session: AsyncSession = Depends(get_db_session, scope="function"),
    _: object = Depends(require_permission("roles:read")),
) -> dict[str, Any]:
    result = await ListRolesUseCase(RoleRepository(session)).execute(params)
    items = [RoleResponse.model_validate(r).model_dump(by_alias=True) for r in result.items]
    return success_envelope(
        items, request_id=get_request_context().request_id, pagination=result.to_meta()
    )


@router.get(
    "/permissions",
    response_model=SuccessEnvelope[list[PermissionResponse]],
    responses=error_responses(
        InvalidTokenError, InsufficientPermissionError, RateLimitedError, validation=True
    ),
)
async def list_permissions(
    params: OffsetParams = Depends(),
    session: AsyncSession = Depends(get_db_session, scope="function"),
    _: object = Depends(require_permission("roles:read")),
) -> dict[str, Any]:
    """The permission catalog a role's `permissions` may draw from, each flagged with whether
    the caller may grant it. Declared before the `/{role_id}` routes so a future
    `GET /{role_id}` can never capture `permissions` as an id."""
    caller_id = get_request_context().user_id
    assert caller_id is not None  # set by get_current_user, a router dependency
    result = await ListPermissionCatalogUseCase(UserRoleRepository(session)).execute(
        caller_id, params
    )
    items = [PermissionResponse.model_validate(p).model_dump(by_alias=True) for p in result.items]
    return success_envelope(
        items, request_id=get_request_context().request_id, pagination=result.to_meta()
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=SuccessEnvelope[RoleResponse],
    dependencies=[Depends(rate_limit(RateLimitTier.WRITE))],
    responses=error_responses(
        InvalidTokenError,
        InsufficientPermissionError,
        RateLimitedError,
        RoleAlreadyExistsError,
        UnknownPermissionError,
        PermissionEscalationError,
        validation=True,
    ),
)
async def create_role(
    body: CreateRoleRequest,
    session: AsyncSession = Depends(get_db_session, scope="function"),
    audit_logs: AuditLogRepositoryPort = Depends(get_audit_log_repository),
    _: object = Depends(require_permission("roles:write")),
) -> dict[str, Any]:
    # tenant_id is NEVER taken from the request body for an authenticated
    # write — always the caller's own tenant, or a "roles:write" caller in
    # Tenant A could create/own a role under an arbitrary Tenant B.
    tenant_id = get_request_context().tenant_id
    assert tenant_id is not None  # guaranteed by get_current_user, which ran as a router dependency
    caller_id = get_request_context().user_id
    assert caller_id is not None  # set by get_current_user, like tenant_id above
    role = await CreateRoleUseCase(RoleRepository(session), UserRoleRepository(session)).execute(
        CreateRoleInput(
            tenant_id=tenant_id,
            name=body.name,
            description=body.description,
            permissions=body.permissions,
        ),
        caller_id,
    )
    await record_audit_event(
        audit_logs,
        action="role.created",
        resource_type="role",
        resource_id=str(role.id),
        after=role.audit_snapshot(),
    )
    payload = RoleResponse.model_validate(role).model_dump(by_alias=True)
    return success_envelope(payload, request_id=get_request_context().request_id)


@router.patch(
    "/{role_id}",
    response_model=SuccessEnvelope[RoleResponse],
    dependencies=[Depends(rate_limit(RateLimitTier.WRITE))],
    responses=error_responses(
        InvalidTokenError,
        InsufficientPermissionError,
        RateLimitedError,
        RoleNotFoundError,
        RoleAlreadyExistsError,
        SystemRoleImmutableError,
        UnknownPermissionError,
        PermissionEscalationError,
        validation=True,
    ),
)
async def update_role(
    role_id: UUID,
    body: UpdateRoleRequest,
    session: AsyncSession = Depends(get_db_session, scope="function"),
    audit_logs: AuditLogRepositoryPort = Depends(get_audit_log_repository),
    _: object = Depends(require_permission("roles:write")),
) -> dict[str, Any]:
    caller_id = get_request_context().user_id
    assert caller_id is not None  # set by get_current_user, a router dependency
    before, role = await UpdateRoleUseCase(
        RoleRepository(session), UserRoleRepository(session)
    ).execute(
        role_id,
        UpdateRoleInput(name=body.name, description=body.description, permissions=body.permissions),
        caller_id,
    )
    if before.audit_snapshot() != role.audit_snapshot():  # an empty/no-op PATCH audits nothing
        await record_audit_event(
            audit_logs,
            action="role.updated",
            resource_type="role",
            resource_id=str(role_id),
            before=before.audit_snapshot(),
            after=role.audit_snapshot(),
        )
    payload = RoleResponse.model_validate(role).model_dump(by_alias=True)
    return success_envelope(payload, request_id=get_request_context().request_id)


@router.delete(
    "/{role_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(rate_limit(RateLimitTier.WRITE))],
    responses=error_responses(
        InvalidTokenError,
        InsufficientPermissionError,
        RateLimitedError,
        RoleNotFoundError,
        SystemRoleImmutableError,
        validation=True,
    ),
)
async def delete_role(
    role_id: UUID,
    session: AsyncSession = Depends(get_db_session, scope="function"),
    audit_logs: AuditLogRepositoryPort = Depends(get_audit_log_repository),
    _: object = Depends(require_permission("roles:delete")),
) -> None:
    before = await DeleteRoleUseCase(RoleRepository(session)).execute(role_id)
    await record_audit_event(
        audit_logs,
        action="role.deleted",
        resource_type="role",
        resource_id=str(role_id),
        before=before.audit_snapshot(),
    )


@router.get(
    "/users/{user_id}",
    response_model=SuccessEnvelope[list[UserRoleResponse]],
    responses=error_responses(
        InvalidTokenError, InsufficientPermissionError, RateLimitedError, validation=True
    ),
)
async def list_user_roles(
    user_id: UUID,
    params: OffsetParams = Depends(),
    session: AsyncSession = Depends(get_db_session, scope="function"),
    _: object = Depends(require_permission("roles:read")),
) -> dict[str, Any]:
    result = await ListUserRolesUseCase(UserRoleRepository(session)).execute(user_id, params)
    items = [
        UserRoleResponse(
            **RoleResponse.model_validate(assignment.role).model_dump(),
            is_default=assignment.is_default,
        ).model_dump(by_alias=True)
        for assignment in result.items
    ]
    return success_envelope(
        items, request_id=get_request_context().request_id, pagination=result.to_meta()
    )


@router.post(
    "/users/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(rate_limit(RateLimitTier.WRITE))],
    responses=error_responses(
        InvalidTokenError,
        InsufficientPermissionError,
        RateLimitedError,
        RoleNotFoundError,
        UserNotFoundError,
        PermissionEscalationError,
        validation=True,
    ),
)
async def assign_role(
    user_id: UUID,
    body: AssignRoleRequest,
    session: AsyncSession = Depends(get_db_session, scope="function"),
    users: UserRepositoryPort = Depends(get_user_repository),
    audit_logs: AuditLogRepositoryPort = Depends(get_audit_log_repository),
    _: object = Depends(require_permission("roles:write")),
) -> None:
    caller_id = get_request_context().user_id
    assert caller_id is not None  # set by get_current_user, a router dependency
    change = await AssignRoleToUserUseCase(
        RoleRepository(session), UserRoleRepository(session), users
    ).execute(user_id, body.role_id, caller_id)
    if change is not None:  # None = already assigned, nothing changed to audit
        await record_audit_event(
            audit_logs,
            action="role.assigned",
            resource_type="user_role",
            resource_id=f"{user_id}:{body.role_id}",
            after=change.audit_snapshot(),
        )


@router.delete(
    "/users/{user_id}/{role_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(rate_limit(RateLimitTier.WRITE))],
    responses=error_responses(
        InvalidTokenError, InsufficientPermissionError, RateLimitedError, validation=True
    ),
)
async def revoke_role(
    user_id: UUID,
    role_id: UUID,
    session: AsyncSession = Depends(get_db_session, scope="function"),
    audit_logs: AuditLogRepositoryPort = Depends(get_audit_log_repository),
    _: object = Depends(require_permission("roles:write")),
) -> None:
    change = await RevokeRoleFromUserUseCase(
        RoleRepository(session), UserRoleRepository(session)
    ).execute(user_id, role_id)
    if change is not None:  # None = role wasn't assigned, nothing changed to audit
        await record_audit_event(
            audit_logs,
            action="role.revoked",
            resource_type="user_role",
            resource_id=f"{user_id}:{role_id}",
            before=change.audit_snapshot(),
        )


@router.post(
    "/users/{user_id}/{role_id}/default",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(rate_limit(RateLimitTier.WRITE))],
    responses=error_responses(
        InvalidTokenError,
        InsufficientPermissionError,
        RateLimitedError,
        RoleNotFoundError,
        RoleNotAssignedError,
        UserNotFoundError,
        validation=True,
    ),
)
async def set_default_role(
    user_id: UUID,
    role_id: UUID,
    session: AsyncSession = Depends(get_db_session, scope="function"),
    users: UserRepositoryPort = Depends(get_user_repository),
    audit_logs: AuditLogRepositoryPort = Depends(get_audit_log_repository),
    _: object = Depends(require_permission("roles:write")),
) -> None:
    user_roles = UserRoleRepository(session)
    previous = await user_roles.get_default_role_for_user(user_id)
    role = await SetDefaultRoleUseCase(RoleRepository(session), user_roles, users).execute(
        user_id, role_id
    )
    await record_audit_event(
        audit_logs,
        action="role.default_set",
        resource_type="user_role",
        resource_id=f"{user_id}:{role_id}",
        before={"defaultRole": previous.name if previous else None},
        after={"defaultRole": role.name},
    )
