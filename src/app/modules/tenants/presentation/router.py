"""Tenant management — intentionally minimal ("skeleton"): a platform-level
super-admin surface for provisioning tenants. Extend with billing plans,
domain mapping, tenant-level settings, etc. as real requirements arrive.

Both permissions here are PLATFORM-ONLY. `tenants:manage` (list/create/update) and
`tenants:delete` (suspend/restore/delete) are held only by the `*` roles (`developer`,
`superadmin`); a tenant `admin` holds neither, because the tenants repository is deliberately
not tenant-scoped — whoever can call these acts on every tenant, not just their own.
(`app.modules.roles.domain.baseline` is the single definition of who holds what.)"""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import get_request_context
from app.core.db import get_db_session
from app.core.di import (
    get_audit_log_repository,
    get_current_user,
    get_tenant_role_provisioner,
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
from app.modules.roles.domain.exceptions import InsufficientPermissionError
from app.modules.tenants.application.use_cases import (
    ActivateTenantUseCase,
    CreateTenantInput,
    CreateTenantUseCase,
    DeactivateTenantUseCase,
    DeleteTenantUseCase,
    ListTenantsUseCase,
    UpdateTenantInput,
    UpdateTenantUseCase,
)
from app.modules.tenants.domain.exceptions import (
    CannotModifyOwnTenantError,
    TenantNotFoundError,
    TenantSlugAlreadyExistsError,
)
from app.modules.tenants.domain.ports import TenantRoleProvisionerPort
from app.modules.tenants.infrastructure.repository import TenantRepository
from app.modules.tenants.presentation.schemas import (
    CreateTenantRequest,
    TenantResponse,
    UpdateTenantRequest,
)

router = APIRouter(
    prefix="/tenants",
    tags=["tenants"],
    dependencies=[
        Depends(rate_limit(RateLimitTier.IP)),
        Depends(get_current_user),
        # Before require_permission, so a 403 still counts against the quota.
        Depends(rate_limit(RateLimitTier.DEFAULT)),
        Depends(require_permission("tenants:manage")),
    ],
)


@router.get(
    "",
    response_model=SuccessEnvelope[list[TenantResponse]],
    responses=error_responses(
        InvalidTokenError, InsufficientPermissionError, RateLimitedError, validation=True
    ),
)
async def list_tenants(
    params: OffsetParams = Depends(),
    session: AsyncSession = Depends(get_db_session, scope="function"),
) -> dict[str, Any]:
    result = await ListTenantsUseCase(TenantRepository(session)).execute(params)
    items = [TenantResponse.model_validate(t).model_dump(by_alias=True) for t in result.items]
    return success_envelope(
        items, request_id=get_request_context().request_id, pagination=result.to_meta()
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=SuccessEnvelope[TenantResponse],
    dependencies=[Depends(rate_limit(RateLimitTier.WRITE))],
    responses=error_responses(
        InvalidTokenError,
        InsufficientPermissionError,
        RateLimitedError,
        TenantSlugAlreadyExistsError,
        validation=True,
    ),
)
async def create_tenant(
    body: CreateTenantRequest,
    session: AsyncSession = Depends(get_db_session, scope="function"),
    role_provisioner: TenantRoleProvisionerPort = Depends(get_tenant_role_provisioner),
    audit_logs: AuditLogRepositoryPort = Depends(get_audit_log_repository),
) -> dict[str, Any]:
    tenant = await CreateTenantUseCase(TenantRepository(session), role_provisioner).execute(
        CreateTenantInput(name=body.name, slug=body.slug)
    )
    await record_audit_event(
        audit_logs,
        action="tenant.created",
        resource_type="tenant",
        resource_id=str(tenant.id),
        after=tenant.audit_snapshot(),
    )
    payload = TenantResponse.model_validate(tenant).model_dump(by_alias=True)
    return success_envelope(payload, request_id=get_request_context().request_id)


@router.patch(
    "/{tenant_id}",
    response_model=SuccessEnvelope[TenantResponse],
    dependencies=[Depends(rate_limit(RateLimitTier.WRITE))],
    responses=error_responses(
        InvalidTokenError,
        InsufficientPermissionError,
        RateLimitedError,
        TenantNotFoundError,
        TenantSlugAlreadyExistsError,
        validation=True,
    ),
)
async def update_tenant(
    tenant_id: UUID,
    body: UpdateTenantRequest,
    session: AsyncSession = Depends(get_db_session, scope="function"),
    audit_logs: AuditLogRepositoryPort = Depends(get_audit_log_repository),
) -> dict[str, Any]:
    before, tenant = await UpdateTenantUseCase(TenantRepository(session)).execute(
        tenant_id, UpdateTenantInput(name=body.name, slug=body.slug)
    )
    if before.audit_snapshot() != tenant.audit_snapshot():  # an empty/no-op PATCH audits nothing
        await record_audit_event(
            audit_logs,
            action="tenant.updated",
            resource_type="tenant",
            resource_id=str(tenant_id),
            before=before.audit_snapshot(),
            after=tenant.audit_snapshot(),
        )
    payload = TenantResponse.model_validate(tenant).model_dump(by_alias=True)
    return success_envelope(payload, request_id=get_request_context().request_id)


@router.post(
    "/{tenant_id}/deactivate",
    response_model=SuccessEnvelope[TenantResponse],
    # Suspend/restore/delete are the destructive tenant-lifecycle operations,
    # gated by "tenants:delete" on top of the router's "tenants:manage": two
    # separate grants so a narrower platform role can be allowed to provision
    # tenants without being able to suspend, un-suspend or delete them. Neither
    # is held by a tenant `admin` (see the module docstring).
    dependencies=[
        Depends(rate_limit(RateLimitTier.WRITE)),
        Depends(require_permission("tenants:delete")),
    ],
    responses=error_responses(
        InvalidTokenError,
        InsufficientPermissionError,
        RateLimitedError,
        TenantNotFoundError,
        CannotModifyOwnTenantError,
        validation=True,
    ),
)
async def deactivate_tenant(
    tenant_id: UUID,
    session: AsyncSession = Depends(get_db_session, scope="function"),
    audit_logs: AuditLogRepositoryPort = Depends(get_audit_log_repository),
) -> dict[str, Any]:
    caller_tenant_id = get_request_context().tenant_id
    assert caller_tenant_id is not None  # guaranteed by get_current_user, a router dependency
    before, tenant = await DeactivateTenantUseCase(TenantRepository(session)).execute(
        tenant_id, caller_tenant_id=caller_tenant_id
    )
    if before.is_active:  # already suspended: nothing changed to audit
        await record_audit_event(
            audit_logs,
            action="tenant.deactivated",
            resource_type="tenant",
            resource_id=str(tenant_id),
            before=before.audit_snapshot(),
            after=tenant.audit_snapshot(),
        )
    payload = TenantResponse.model_validate(tenant).model_dump(by_alias=True)
    return success_envelope(payload, request_id=get_request_context().request_id)


@router.post(
    "/{tenant_id}/activate",
    response_model=SuccessEnvelope[TenantResponse],
    # Same gate as deactivate: otherwise any tenant admin could lift a suspension.
    dependencies=[
        Depends(rate_limit(RateLimitTier.WRITE)),
        Depends(require_permission("tenants:delete")),
    ],
    responses=error_responses(
        InvalidTokenError,
        InsufficientPermissionError,
        RateLimitedError,
        TenantNotFoundError,
        validation=True,
    ),
)
async def activate_tenant(
    tenant_id: UUID,
    session: AsyncSession = Depends(get_db_session, scope="function"),
    audit_logs: AuditLogRepositoryPort = Depends(get_audit_log_repository),
) -> dict[str, Any]:
    before, tenant = await ActivateTenantUseCase(TenantRepository(session)).execute(tenant_id)
    if not before.is_active:  # already active: nothing changed to audit
        await record_audit_event(
            audit_logs,
            action="tenant.activated",
            resource_type="tenant",
            resource_id=str(tenant_id),
            before=before.audit_snapshot(),
            after=tenant.audit_snapshot(),
        )
    payload = TenantResponse.model_validate(tenant).model_dump(by_alias=True)
    return success_envelope(payload, request_id=get_request_context().request_id)


@router.delete(
    "/{tenant_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    # On top of the router's "tenants:manage" (see the module docstring: both
    # are platform-only, never held by a tenant `admin`).
    dependencies=[
        Depends(rate_limit(RateLimitTier.WRITE)),
        Depends(require_permission("tenants:delete")),
    ],
    responses=error_responses(
        InvalidTokenError,
        InsufficientPermissionError,
        RateLimitedError,
        TenantNotFoundError,
        CannotModifyOwnTenantError,
        validation=True,
    ),
)
async def delete_tenant(
    tenant_id: UUID,
    session: AsyncSession = Depends(get_db_session, scope="function"),
    audit_logs: AuditLogRepositoryPort = Depends(get_audit_log_repository),
) -> None:
    caller_tenant_id = get_request_context().tenant_id
    assert caller_tenant_id is not None  # guaranteed by get_current_user, a router dependency
    before = await DeleteTenantUseCase(TenantRepository(session)).execute(
        tenant_id, caller_tenant_id=caller_tenant_id
    )
    await record_audit_event(
        audit_logs,
        action="tenant.deleted",
        resource_type="tenant",
        resource_id=str(tenant_id),
        before=before.audit_snapshot(),
    )
