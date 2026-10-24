from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import get_request_context
from app.core.db import get_db_session
from app.core.di import get_current_user, require_permission
from app.core.errors import error_responses
from app.core.exceptions import RateLimitedError
from app.core.pagination import OffsetParams
from app.core.rate_limit import RateLimitTier, rate_limit
from app.core.response import SuccessEnvelope, success_envelope
from app.modules.audit_log.application.use_cases import ListAuditLogsUseCase
from app.modules.audit_log.infrastructure.repository import AuditLogRepository
from app.modules.audit_log.presentation.schemas import AuditLogEntryResponse
from app.modules.auth.domain.exceptions import InvalidTokenError
from app.modules.roles.domain.exceptions import InsufficientPermissionError

router = APIRouter(
    prefix="/audit-logs",
    tags=["audit-log"],
    dependencies=[
        Depends(rate_limit(RateLimitTier.IP)),
        Depends(get_current_user),
        # Before require_permission, so a 403 still counts against the quota.
        Depends(rate_limit(RateLimitTier.DEFAULT)),
        Depends(require_permission("audit_log:read")),
    ],
)


@router.get(
    "",
    response_model=SuccessEnvelope[list[AuditLogEntryResponse]],
    responses=error_responses(
        InvalidTokenError, InsufficientPermissionError, RateLimitedError, validation=True
    ),
)
async def list_audit_logs(
    params: OffsetParams = Depends(),
    resource_type: str | None = None,
    actor_id: UUID | None = None,
    session: AsyncSession = Depends(get_db_session, scope="function"),
) -> dict[str, Any]:
    result = await ListAuditLogsUseCase(AuditLogRepository(session)).execute(
        params, resource_type=resource_type, actor_id=actor_id
    )
    items = [
        AuditLogEntryResponse.model_validate(e).model_dump(by_alias=True) for e in result.items
    ]
    return success_envelope(
        items, request_id=get_request_context().request_id, pagination=result.to_meta()
    )
