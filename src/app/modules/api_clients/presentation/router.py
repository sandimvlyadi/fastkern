"""Human-authenticated lifecycle management for partner API keys. See
`app.modules.partners.presentation.router` for the API-key-authenticated
side partners actually call — that router never does RBAC/session auth,
only a static `X-API-Key` lookup."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, status

from app.core.config import Settings, get_settings
from app.core.context import get_request_context
from app.core.di import (
    get_api_key_repository,
    get_audit_log_repository,
    get_current_user,
    get_user_role_repository,
    record_audit_event,
    require_permission,
)
from app.core.errors import error_responses
from app.core.exceptions import RateLimitedError
from app.core.pagination import OffsetParams
from app.core.rate_limit import RateLimitTier, rate_limit
from app.core.response import SuccessEnvelope, success_envelope
from app.modules.api_clients.application.dto import CreateApiKeyInput
from app.modules.api_clients.application.use_cases import (
    CreateApiKeyUseCase,
    ListApiKeyScopesUseCase,
    ListApiKeysUseCase,
    RevokeApiKeyUseCase,
    RotateApiKeyUseCase,
)
from app.modules.api_clients.domain.exceptions import (
    ApiClientNotFoundError,
    ApiKeyAlreadyExpiredError,
    ApiKeyAlreadyRevokedError,
    ScopeEscalationError,
    UnknownScopeError,
)
from app.modules.api_clients.domain.ports import ApiKeyRepositoryPort
from app.modules.api_clients.presentation.schemas import (
    ApiKeyCreatedResponse,
    ApiKeyResponse,
    ApiKeyScopeResponse,
    CreateApiKeyRequest,
)
from app.modules.audit_log.domain.ports import AuditLogRepositoryPort
from app.modules.auth.domain.exceptions import InvalidTokenError
from app.modules.roles.domain.exceptions import InsufficientPermissionError
from app.modules.roles.domain.ports import UserRoleRepositoryPort

router = APIRouter(
    prefix="/api-clients",
    tags=["api-clients"],
    dependencies=[
        Depends(rate_limit(RateLimitTier.IP)),  # first: counts requests auth rejects
        Depends(get_current_user),
        Depends(rate_limit(RateLimitTier.DEFAULT)),
        Depends(require_permission("api_clients:read")),
    ],
)


@router.get(
    "",
    response_model=SuccessEnvelope[list[ApiKeyResponse]],
    responses=error_responses(
        InvalidTokenError, InsufficientPermissionError, RateLimitedError, validation=True
    ),
)
async def list_api_keys(
    params: OffsetParams = Depends(),
    api_keys: ApiKeyRepositoryPort = Depends(get_api_key_repository),
) -> dict[str, Any]:
    result = await ListApiKeysUseCase(api_keys).execute(params)
    items = [ApiKeyResponse.model_validate(k).model_dump(by_alias=True) for k in result.items]
    return success_envelope(
        items, request_id=get_request_context().request_id, pagination=result.to_meta()
    )


@router.get(
    "/scopes",
    response_model=SuccessEnvelope[list[ApiKeyScopeResponse]],
    responses=error_responses(
        InvalidTokenError, InsufficientPermissionError, RateLimitedError, validation=True
    ),
)
async def list_api_key_scopes(
    params: OffsetParams = Depends(),
    user_roles: UserRoleRepositoryPort = Depends(get_user_role_repository),
) -> dict[str, Any]:
    """The scope catalog an API key's `scopes` may draw from, each flagged with whether the
    caller may grant it."""
    caller_id = get_request_context().user_id
    assert caller_id is not None  # set by get_current_user, a router dependency
    result = await ListApiKeyScopesUseCase(user_roles).execute(caller_id, params)
    items = [ApiKeyScopeResponse.model_validate(s).model_dump(by_alias=True) for s in result.items]
    return success_envelope(
        items, request_id=get_request_context().request_id, pagination=result.to_meta()
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=SuccessEnvelope[ApiKeyCreatedResponse],
    dependencies=[
        Depends(rate_limit(RateLimitTier.WRITE)),
        Depends(require_permission("api_clients:write")),
    ],
    responses=error_responses(
        InvalidTokenError,
        InsufficientPermissionError,
        RateLimitedError,
        UnknownScopeError,
        ScopeEscalationError,
        validation=True,
    ),
)
async def create_api_key(
    body: CreateApiKeyRequest,
    api_keys: ApiKeyRepositoryPort = Depends(get_api_key_repository),
    user_roles: UserRoleRepositoryPort = Depends(get_user_role_repository),
    audit_logs: AuditLogRepositoryPort = Depends(get_audit_log_repository),
) -> dict[str, Any]:
    ctx = get_request_context()
    # Both guaranteed by get_current_user, a router dependency.
    assert ctx.tenant_id is not None
    assert ctx.user_id is not None
    api_key, plaintext = await CreateApiKeyUseCase(api_keys, user_roles).execute(
        CreateApiKeyInput(
            tenant_id=ctx.tenant_id, name=body.name, scopes=body.scopes, expires_at=body.expires_at
        ),
        caller_id=ctx.user_id,
    )
    await record_audit_event(
        audit_logs,
        action="api_client.created",
        resource_type="api_client",
        resource_id=str(api_key.id),
        after=api_key.audit_snapshot(),
    )
    payload = ApiKeyCreatedResponse(
        **ApiKeyResponse.model_validate(api_key).model_dump(), plaintext_key=plaintext
    ).model_dump(by_alias=True)
    return success_envelope(payload, request_id=ctx.request_id)


@router.post(
    "/{api_key_id}/rotate",
    response_model=SuccessEnvelope[ApiKeyCreatedResponse],
    dependencies=[
        Depends(rate_limit(RateLimitTier.WRITE)),
        Depends(require_permission("api_clients:write")),
    ],
    responses=error_responses(
        InvalidTokenError,
        InsufficientPermissionError,
        RateLimitedError,
        ApiClientNotFoundError,
        ApiKeyAlreadyRevokedError,
        ApiKeyAlreadyExpiredError,
        ScopeEscalationError,
        validation=True,
    ),
)
async def rotate_api_key(
    api_key_id: UUID,
    api_keys: ApiKeyRepositoryPort = Depends(get_api_key_repository),
    user_roles: UserRoleRepositoryPort = Depends(get_user_role_repository),
    audit_logs: AuditLogRepositoryPort = Depends(get_audit_log_repository),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    ctx = get_request_context()
    assert ctx.user_id is not None  # set by get_current_user, a router dependency
    api_key, plaintext = await RotateApiKeyUseCase(api_keys, user_roles).execute(
        api_key_id,
        caller_id=ctx.user_id,
        overlap_hours=settings.api_key_rotation_overlap_hours,
    )
    await record_audit_event(
        audit_logs,
        action="api_client.rotated",
        resource_type="api_client",
        resource_id=str(api_key_id),
        after=api_key.audit_snapshot(),
    )
    payload = ApiKeyCreatedResponse(
        **ApiKeyResponse.model_validate(api_key).model_dump(), plaintext_key=plaintext
    ).model_dump(by_alias=True)
    return success_envelope(payload, request_id=ctx.request_id)


@router.post(
    "/{api_key_id}/revoke",
    response_model=SuccessEnvelope[ApiKeyResponse],
    dependencies=[
        Depends(rate_limit(RateLimitTier.WRITE)),
        Depends(require_permission("api_clients:write")),
    ],
    responses=error_responses(
        InvalidTokenError,
        InsufficientPermissionError,
        RateLimitedError,
        ApiClientNotFoundError,
        validation=True,
    ),
)
async def revoke_api_key(
    api_key_id: UUID,
    api_keys: ApiKeyRepositoryPort = Depends(get_api_key_repository),
    audit_logs: AuditLogRepositoryPort = Depends(get_audit_log_repository),
) -> dict[str, Any]:
    api_key = await RevokeApiKeyUseCase(api_keys).execute(api_key_id)
    await record_audit_event(
        audit_logs,
        action="api_client.revoked",
        resource_type="api_client",
        resource_id=str(api_key_id),
        after=api_key.audit_snapshot(),
    )
    payload = ApiKeyResponse.model_validate(api_key).model_dump(by_alias=True)
    return success_envelope(payload, request_id=get_request_context().request_id)
