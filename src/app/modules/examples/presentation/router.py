"""Transaction Example: a business-transaction CRUD example with a nullable reference to a
Master Example and an owned list of items embedded in the payload. See docs/module-guide.md."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import get_request_context
from app.core.db import get_db_session
from app.core.di import (
    get_audit_log_repository,
    get_current_user,
    get_master_example_repository,
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
from app.modules.examples.application.dto import CreateExampleInput, UpdateExampleInput
from app.modules.examples.application.use_cases import (
    CreateExampleUseCase,
    DeleteExampleUseCase,
    GetExampleUseCase,
    ListExamplesUseCase,
    UpdateExampleUseCase,
)
from app.modules.examples.domain.exceptions import ExampleNotFoundError
from app.modules.examples.infrastructure.repository import ExampleRepository
from app.modules.examples.presentation.schemas import (
    CreateExampleRequest,
    ExampleResponse,
    UpdateExampleRequest,
)
from app.modules.master_examples.domain.exceptions import MasterExampleNotFoundError
from app.modules.master_examples.domain.ports import MasterExampleRepositoryPort
from app.modules.roles.domain.exceptions import InsufficientPermissionError

router = APIRouter(
    prefix="/examples",
    tags=["examples"],
    dependencies=[
        Depends(rate_limit(RateLimitTier.IP)),  # first: counts requests auth rejects
        Depends(get_current_user),
        Depends(rate_limit(RateLimitTier.DEFAULT)),
    ],
)


@router.get(
    "",
    response_model=SuccessEnvelope[list[ExampleResponse]],
    dependencies=[Depends(require_permission("examples:read"))],
    responses=error_responses(
        InvalidTokenError, InsufficientPermissionError, RateLimitedError, validation=True
    ),
)
async def list_examples(
    params: OffsetParams = Depends(),
    session: AsyncSession = Depends(get_db_session, scope="function"),
) -> dict[str, Any]:
    result = await ListExamplesUseCase(ExampleRepository(session)).execute(params)
    items = [ExampleResponse.model_validate(e).model_dump(by_alias=True) for e in result.items]
    return success_envelope(
        items, request_id=get_request_context().request_id, pagination=result.to_meta()
    )


@router.get(
    "/{example_id}",
    response_model=SuccessEnvelope[ExampleResponse],
    dependencies=[Depends(require_permission("examples:read"))],
    responses=error_responses(
        InvalidTokenError,
        InsufficientPermissionError,
        RateLimitedError,
        ExampleNotFoundError,
        validation=True,
    ),
)
async def get_example(
    example_id: UUID, session: AsyncSession = Depends(get_db_session, scope="function")
) -> dict[str, Any]:
    example = await GetExampleUseCase(ExampleRepository(session)).execute(example_id)
    payload = ExampleResponse.model_validate(example).model_dump(by_alias=True)
    return success_envelope(payload, request_id=get_request_context().request_id)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=SuccessEnvelope[ExampleResponse],
    dependencies=[
        Depends(rate_limit(RateLimitTier.WRITE)),
        Depends(require_permission("examples:write")),
    ],
    responses=error_responses(
        InvalidTokenError,
        InsufficientPermissionError,
        RateLimitedError,
        MasterExampleNotFoundError,
        validation=True,
    ),
)
async def create_example(
    body: CreateExampleRequest,
    session: AsyncSession = Depends(get_db_session, scope="function"),
    audit_logs: AuditLogRepositoryPort = Depends(get_audit_log_repository),
    master_examples: MasterExampleRepositoryPort = Depends(get_master_example_repository),
) -> dict[str, Any]:
    ctx = get_request_context()
    assert ctx.tenant_id is not None  # guaranteed by get_current_user, a router dependency
    example = await CreateExampleUseCase(ExampleRepository(session), master_examples).execute(
        CreateExampleInput(
            name=body.name,
            description=body.description,
            master_example_id=body.master_example_id,
            items=body.items,
        ),
        ctx.tenant_id,
    )
    await record_audit_event(
        audit_logs,
        action="example.created",
        resource_type="example",
        resource_id=str(example.id),
        after=example.audit_snapshot(),
    )
    payload = ExampleResponse.model_validate(example).model_dump(by_alias=True)
    return success_envelope(payload, request_id=ctx.request_id)


@router.patch(
    "/{example_id}",
    response_model=SuccessEnvelope[ExampleResponse],
    dependencies=[
        Depends(rate_limit(RateLimitTier.WRITE)),
        Depends(require_permission("examples:write")),
    ],
    responses=error_responses(
        InvalidTokenError,
        InsufficientPermissionError,
        RateLimitedError,
        ExampleNotFoundError,
        MasterExampleNotFoundError,
        validation=True,
    ),
)
async def update_example(
    example_id: UUID,
    body: UpdateExampleRequest,
    session: AsyncSession = Depends(get_db_session, scope="function"),
    audit_logs: AuditLogRepositoryPort = Depends(get_audit_log_repository),
    master_examples: MasterExampleRepositoryPort = Depends(get_master_example_repository),
) -> dict[str, Any]:
    # exclude_unset keeps "omitted" apart from "cleared" (see UpdateExampleInput).
    before, example = await UpdateExampleUseCase(
        ExampleRepository(session), master_examples
    ).execute(example_id, UpdateExampleInput(**body.model_dump(exclude_unset=True)))
    if before.audit_snapshot() != example.audit_snapshot():  # an empty/no-op PATCH audits nothing
        await record_audit_event(
            audit_logs,
            action="example.updated",
            resource_type="example",
            resource_id=str(example_id),
            before=before.audit_snapshot(),
            after=example.audit_snapshot(),
        )
    payload = ExampleResponse.model_validate(example).model_dump(by_alias=True)
    return success_envelope(payload, request_id=get_request_context().request_id)


@router.delete(
    "/{example_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[
        Depends(rate_limit(RateLimitTier.WRITE)),
        Depends(require_permission("examples:delete")),
    ],
    responses=error_responses(
        InvalidTokenError,
        InsufficientPermissionError,
        RateLimitedError,
        ExampleNotFoundError,
        validation=True,
    ),
)
async def delete_example(
    example_id: UUID,
    session: AsyncSession = Depends(get_db_session, scope="function"),
    audit_logs: AuditLogRepositoryPort = Depends(get_audit_log_repository),
) -> None:
    before = await DeleteExampleUseCase(ExampleRepository(session)).execute(example_id)
    await record_audit_event(
        audit_logs,
        action="example.deleted",
        resource_type="example",
        resource_id=str(example_id),
        before=before.audit_snapshot(),
    )
