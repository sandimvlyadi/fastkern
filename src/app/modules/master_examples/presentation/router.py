"""Master Example: a reference-data CRUD example showing full CRUD, soft delete, pagination,
RBAC, rate limiting, audit logging, i18n and OpenAPI documentation. See docs/module-guide.md."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import get_request_context
from app.core.db import get_db_session
from app.core.di import (
    get_audit_log_repository,
    get_current_user,
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
from app.modules.master_examples.application.dto import (
    CreateMasterExampleInput,
    UpdateMasterExampleInput,
)
from app.modules.master_examples.application.use_cases import (
    CreateMasterExampleUseCase,
    DeleteMasterExampleUseCase,
    GetMasterExampleUseCase,
    ListMasterExamplesUseCase,
    UpdateMasterExampleUseCase,
)
from app.modules.master_examples.domain.exceptions import (
    MasterExampleAlreadyExistsError,
    MasterExampleNotFoundError,
)
from app.modules.master_examples.infrastructure.repository import MasterExampleRepository
from app.modules.master_examples.presentation.schemas import (
    CreateMasterExampleRequest,
    MasterExampleResponse,
    UpdateMasterExampleRequest,
)
from app.modules.roles.domain.exceptions import InsufficientPermissionError

router = APIRouter(
    prefix="/master/examples",
    tags=["master-examples"],
    dependencies=[
        Depends(rate_limit(RateLimitTier.IP)),  # first: counts requests auth rejects
        Depends(get_current_user),
        Depends(rate_limit(RateLimitTier.DEFAULT)),
    ],
)


@router.get(
    "",
    response_model=SuccessEnvelope[list[MasterExampleResponse]],
    dependencies=[Depends(require_permission("master_examples:read"))],
    responses=error_responses(
        InvalidTokenError, InsufficientPermissionError, RateLimitedError, validation=True
    ),
)
async def list_master_examples(
    params: OffsetParams = Depends(),
    session: AsyncSession = Depends(get_db_session, scope="function"),
) -> dict[str, Any]:
    result = await ListMasterExamplesUseCase(MasterExampleRepository(session)).execute(params)
    items = [
        MasterExampleResponse.model_validate(e).model_dump(by_alias=True) for e in result.items
    ]
    return success_envelope(
        items, request_id=get_request_context().request_id, pagination=result.to_meta()
    )


@router.get(
    "/{example_id}",
    response_model=SuccessEnvelope[MasterExampleResponse],
    dependencies=[Depends(require_permission("master_examples:read"))],
    responses=error_responses(
        InvalidTokenError,
        InsufficientPermissionError,
        RateLimitedError,
        MasterExampleNotFoundError,
        validation=True,
    ),
)
async def get_master_example(
    example_id: UUID, session: AsyncSession = Depends(get_db_session, scope="function")
) -> dict[str, Any]:
    example = await GetMasterExampleUseCase(MasterExampleRepository(session)).execute(example_id)
    payload = MasterExampleResponse.model_validate(example).model_dump(by_alias=True)
    return success_envelope(payload, request_id=get_request_context().request_id)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=SuccessEnvelope[MasterExampleResponse],
    dependencies=[
        Depends(rate_limit(RateLimitTier.WRITE)),
        Depends(require_permission("master_examples:write")),
    ],
    responses=error_responses(
        InvalidTokenError,
        InsufficientPermissionError,
        RateLimitedError,
        MasterExampleAlreadyExistsError,
        validation=True,
    ),
)
async def create_master_example(
    body: CreateMasterExampleRequest,
    session: AsyncSession = Depends(get_db_session, scope="function"),
    audit_logs: AuditLogRepositoryPort = Depends(get_audit_log_repository),
) -> dict[str, Any]:
    ctx = get_request_context()
    assert ctx.tenant_id is not None  # guaranteed by get_current_user, a router dependency
    example = await CreateMasterExampleUseCase(MasterExampleRepository(session)).execute(
        CreateMasterExampleInput(name=body.name, description=body.description), ctx.tenant_id
    )
    await record_audit_event(
        audit_logs,
        action="master_example.created",
        resource_type="master_example",
        resource_id=str(example.id),
        after=example.audit_snapshot(),
    )
    payload = MasterExampleResponse.model_validate(example).model_dump(by_alias=True)
    return success_envelope(payload, request_id=ctx.request_id)


@router.patch(
    "/{example_id}",
    response_model=SuccessEnvelope[MasterExampleResponse],
    dependencies=[
        Depends(rate_limit(RateLimitTier.WRITE)),
        Depends(require_permission("master_examples:write")),
    ],
    responses=error_responses(
        InvalidTokenError,
        InsufficientPermissionError,
        RateLimitedError,
        MasterExampleNotFoundError,
        MasterExampleAlreadyExistsError,
        validation=True,
    ),
)
async def update_master_example(
    example_id: UUID,
    body: UpdateMasterExampleRequest,
    session: AsyncSession = Depends(get_db_session, scope="function"),
    audit_logs: AuditLogRepositoryPort = Depends(get_audit_log_repository),
) -> dict[str, Any]:
    before, example = await UpdateMasterExampleUseCase(MasterExampleRepository(session)).execute(
        example_id,
        UpdateMasterExampleInput(**body.model_dump(exclude_unset=True)),
    )
    if before.audit_snapshot() != example.audit_snapshot():  # an empty/no-op PATCH audits nothing
        await record_audit_event(
            audit_logs,
            action="master_example.updated",
            resource_type="master_example",
            resource_id=str(example_id),
            before=before.audit_snapshot(),
            after=example.audit_snapshot(),
        )
    payload = MasterExampleResponse.model_validate(example).model_dump(by_alias=True)
    return success_envelope(payload, request_id=get_request_context().request_id)


@router.delete(
    "/{example_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[
        Depends(rate_limit(RateLimitTier.WRITE)),
        Depends(require_permission("master_examples:delete")),
    ],
    responses=error_responses(
        InvalidTokenError,
        InsufficientPermissionError,
        RateLimitedError,
        MasterExampleNotFoundError,
        validation=True,
    ),
)
async def delete_master_example(
    example_id: UUID,
    session: AsyncSession = Depends(get_db_session, scope="function"),
    audit_logs: AuditLogRepositoryPort = Depends(get_audit_log_repository),
) -> None:
    before = await DeleteMasterExampleUseCase(MasterExampleRepository(session)).execute(example_id)
    await record_audit_event(
        audit_logs,
        action="master_example.deleted",
        resource_type="master_example",
        resource_id=str(example_id),
        before=before.audit_snapshot(),
    )
