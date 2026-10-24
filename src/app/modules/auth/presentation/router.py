from dataclasses import asdict
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, status

from app.core.celery_app import dispatch_task
from app.core.config import Settings, get_settings
from app.core.context import get_request_context, update_request_context
from app.core.di import (
    get_audit_log_repository,
    get_current_user,
    get_role_repository,
    get_tenant_repository,
    get_token_session_store,
    get_user_repository,
    get_user_role_repository,
    record_audit_event,
)
from app.core.errors import error_responses
from app.core.exceptions import RateLimitedError
from app.core.pagination import OffsetParams
from app.core.rate_limit import RateLimitTier, rate_limit
from app.core.response import SuccessEnvelope, success_envelope
from app.modules.audit_log.domain.ports import AuditLogRepositoryPort
from app.modules.auth.application.dto import LoginInput, RegisterInput
from app.modules.auth.application.use_cases import (
    LoginUseCase,
    LogoutUseCase,
    RefreshTokenUseCase,
    RegisterUseCase,
    SwitchRoleUseCase,
)
from app.modules.auth.domain.exceptions import (
    AccountDisabledError,
    InvalidCredentialsError,
    InvalidTokenError,
    TokenRevokedError,
)
from app.modules.auth.infrastructure.token_session_store import RedisTokenSessionStore
from app.modules.auth.presentation.schemas import (
    LoginRequest,
    LogoutRequest,
    MeResponse,
    MyRoleResponse,
    RefreshRequest,
    RegisterRequest,
    SwitchRoleRequest,
    TokenResponse,
    UserSummaryResponse,
)
from app.modules.roles.application.use_cases import SwitchActiveRoleUseCase
from app.modules.roles.domain.exceptions import RoleNotAssignedError
from app.modules.roles.domain.ports import RoleRepositoryPort, UserRoleRepositoryPort
from app.modules.tenants.domain.exceptions import TenantInactiveError, TenantNotFoundError
from app.modules.tenants.domain.ports import TenantRepositoryPort
from app.modules.users.domain.entities import User
from app.modules.users.domain.exceptions import EmailAlreadyRegisteredError
from app.modules.users.domain.ports import UserRepositoryPort

# The IP tier is router-level so it runs before every endpoint's own
# dependencies (including `get_current_user` on /me and /switch-role).
router = APIRouter(
    prefix="/auth", tags=["auth"], dependencies=[Depends(rate_limit(RateLimitTier.IP))]
)


def _avatar_url(avatar_file_id: UUID | None) -> str | None:
    # Deliberately duplicates `users.presentation.schemas.avatar_url` (3 lines) rather than
    # sharing it across these two modules' presentation layers.
    if avatar_file_id is None:
        return None
    return f"{get_settings().api_v1_prefix}/public/files/{avatar_file_id}/original"


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    response_model=SuccessEnvelope[UserSummaryResponse],
    dependencies=[Depends(rate_limit(RateLimitTier.AUTH))],
    responses=error_responses(
        RateLimitedError, TenantNotFoundError, EmailAlreadyRegisteredError, validation=True
    ),
)
async def register(
    body: RegisterRequest,
    users: UserRepositoryPort = Depends(get_user_repository),
    roles: RoleRepositoryPort = Depends(get_role_repository),
    user_roles: UserRoleRepositoryPort = Depends(get_user_role_repository),
    tenants: TenantRepositoryPort = Depends(get_tenant_repository),
    audit_logs: AuditLogRepositoryPort = Depends(get_audit_log_repository),
) -> dict[str, Any]:
    user = await RegisterUseCase(users, roles, user_roles, tenants).execute(
        RegisterInput(
            tenant_id=body.tenant_id,
            email=body.email,
            password=body.password,
            full_name=body.full_name,
        )
    )
    # No JWT exists yet at this point (pre-auth flow), so ambient context
    # isn't populated the way get_current_user would populate it for every
    # other mutating endpoint — set it from the user we just created so
    # record_audit_event below attributes the event to the right actor/tenant.
    update_request_context(tenant_id=user.tenant_id, user_id=user.id)
    await record_audit_event(
        audit_logs,
        action="user.registered",
        resource_type="user",
        resource_id=str(user.id),
        after=user.audit_snapshot(),
    )
    # By task name, not by importing the notifications module, so the two stay decoupled.
    # The email's language is the new user's saved locale, not the caller's Accept-Language.
    await dispatch_task(
        "notifications.send_welcome_email",
        to=user.email,
        full_name=user.full_name,
        locale=user.locale,
    )
    payload = UserSummaryResponse.model_validate(user).model_dump(by_alias=True)
    return success_envelope(payload, request_id=get_request_context().request_id)


@router.post(
    "/login",
    response_model=SuccessEnvelope[TokenResponse],
    dependencies=[Depends(rate_limit(RateLimitTier.AUTH))],
    responses=error_responses(
        InvalidCredentialsError,
        AccountDisabledError,
        RateLimitedError,
        TenantInactiveError,
        validation=True,
    ),
)
async def login(
    body: LoginRequest,
    users: UserRepositoryPort = Depends(get_user_repository),
    user_roles: UserRoleRepositoryPort = Depends(get_user_role_repository),
    tenants: TenantRepositoryPort = Depends(get_tenant_repository),
    settings: Settings = Depends(get_settings),
    sessions: RedisTokenSessionStore = Depends(get_token_session_store),
    audit_logs: AuditLogRepositoryPort = Depends(get_audit_log_repository),
) -> dict[str, Any]:
    user, tokens = await LoginUseCase(users, user_roles, settings, sessions, tenants).execute(
        LoginInput(tenant_id=body.tenant_id, email=body.email, password=body.password)
    )
    # Pre-auth flow, same as register: no JWT was presented, so populate the
    # ambient context from the authenticated user before auditing. A login
    # has no before/after state to diff — the event itself is the record.
    update_request_context(tenant_id=user.tenant_id, user_id=user.id)
    await record_audit_event(
        audit_logs, action="auth.login", resource_type="user", resource_id=str(user.id)
    )
    payload = TokenResponse(**asdict(tokens)).model_dump(by_alias=True)
    return success_envelope(payload, request_id=get_request_context().request_id)


@router.post(
    "/refresh",
    response_model=SuccessEnvelope[TokenResponse],
    dependencies=[Depends(rate_limit(RateLimitTier.AUTH))],
    responses=error_responses(
        InvalidTokenError,
        RateLimitedError,
        TokenRevokedError,
        TenantInactiveError,
        validation=True,
    ),
)
async def refresh(
    body: RefreshRequest,
    users: UserRepositoryPort = Depends(get_user_repository),
    tenants: TenantRepositoryPort = Depends(get_tenant_repository),
    user_roles: UserRoleRepositoryPort = Depends(get_user_role_repository),
    settings: Settings = Depends(get_settings),
    sessions: RedisTokenSessionStore = Depends(get_token_session_store),
) -> dict[str, Any]:
    tokens = await RefreshTokenUseCase(users, settings, sessions, tenants, user_roles).execute(
        body.refresh_token
    )
    payload = TokenResponse(**asdict(tokens)).model_dump(by_alias=True)
    return success_envelope(payload, request_id=get_request_context().request_id)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(rate_limit(RateLimitTier.DEFAULT))],
    responses=error_responses(RateLimitedError, validation=True),
)
async def logout(
    body: LogoutRequest,
    settings: Settings = Depends(get_settings),
    sessions: RedisTokenSessionStore = Depends(get_token_session_store),
    audit_logs: AuditLogRepositoryPort = Depends(get_audit_log_repository),
) -> None:
    payload = await LogoutUseCase(settings, sessions).execute(body.refresh_token)
    if payload is not None:
        update_request_context(tenant_id=payload.tenant_id, user_id=payload.subject)
        await record_audit_event(
            audit_logs,
            action="auth.logout",
            resource_type="user",
            resource_id=str(payload.subject),
        )


@router.post(
    "/switch-role",
    response_model=SuccessEnvelope[TokenResponse],
    # get_current_user is listed first so the limiter keys on tenant/user, not
    # just IP (endpoint `dependencies` run before parameter dependencies);
    # FastAPI caches it, so the `current_user` parameter below reuses the result.
    dependencies=[
        Depends(get_current_user),
        Depends(rate_limit(RateLimitTier.DEFAULT)),
        Depends(rate_limit(RateLimitTier.WRITE)),
    ],
    responses=error_responses(
        InvalidTokenError, RateLimitedError, RoleNotAssignedError, validation=True
    ),
)
async def switch_role(
    body: SwitchRoleRequest,
    current_user: User = Depends(get_current_user),
    users: UserRepositoryPort = Depends(get_user_repository),
    user_roles: UserRoleRepositoryPort = Depends(get_user_role_repository),
    settings: Settings = Depends(get_settings),
    sessions: RedisTokenSessionStore = Depends(get_token_session_store),
    audit_logs: AuditLogRepositoryPort = Depends(get_audit_log_repository),
) -> dict[str, Any]:
    """Reissues an access+refresh pair with a different `active_role`,
    without a full re-login. The requested role must already be assigned
    to the caller (see roles module)."""
    previous_role = get_request_context().active_role
    switch_active_role = SwitchActiveRoleUseCase(user_roles)
    tokens = await SwitchRoleUseCase(users, settings, sessions, switch_active_role).execute(
        current_user.id, body.role_name, session_id=get_request_context().session_id
    )
    await record_audit_event(
        audit_logs,
        action="auth.role_switched",
        resource_type="user",
        resource_id=str(current_user.id),
        before={"activeRole": previous_role},
        after={"activeRole": body.role_name},
    )
    payload = TokenResponse(**asdict(tokens)).model_dump(by_alias=True)
    return success_envelope(payload, request_id=get_request_context().request_id)


@router.get(
    "/me",
    response_model=SuccessEnvelope[MeResponse],
    dependencies=[Depends(get_current_user), Depends(rate_limit(RateLimitTier.DEFAULT))],
    responses=error_responses(InvalidTokenError, RateLimitedError),
)
async def me(
    current_user: User = Depends(get_current_user),
    user_roles: UserRoleRepositoryPort = Depends(get_user_role_repository),
) -> dict[str, Any]:
    payload = UserSummaryResponse.model_validate(current_user).model_dump(by_alias=True)
    payload["avatarUrl"] = _avatar_url(current_user.avatar_file_id)
    # The token's `active_role` claim outlives a revoked assignment: only report it while it is
    # still one of the caller's roles, so this never advertises a role they no longer hold.
    active_role = get_request_context().active_role
    if active_role is not None and not await user_roles.user_has_role(current_user.id, active_role):
        active_role = None
    payload["activeRole"] = active_role
    return success_envelope(payload, request_id=get_request_context().request_id)


@router.get(
    "/me/roles",
    response_model=SuccessEnvelope[list[MyRoleResponse]],
    dependencies=[Depends(get_current_user), Depends(rate_limit(RateLimitTier.DEFAULT))],
    responses=error_responses(InvalidTokenError, RateLimitedError, validation=True),
)
async def my_roles(
    params: OffsetParams = Depends(),
    current_user: User = Depends(get_current_user),
    user_roles: UserRoleRepositoryPort = Depends(get_user_role_repository),
) -> dict[str, Any]:
    """Self-service: the caller's own assigned roles, with which one is
    their default — so a client can build a role-switch UI without needing
    `roles:read` (that's only granted on `GET /roles/users/{user_id}`,
    admin-only)."""
    result = await user_roles.paginate_roles_for_user(current_user.id, params)
    items = [
        MyRoleResponse(
            id=assignment.role.id,
            name=assignment.role.name,
            description=assignment.role.description,
            permissions=assignment.role.permissions,
            is_default=assignment.is_default,
        ).model_dump(by_alias=True)
        for assignment in result.items
    ]
    return success_envelope(
        items, request_id=get_request_context().request_id, pagination=result.to_meta()
    )
