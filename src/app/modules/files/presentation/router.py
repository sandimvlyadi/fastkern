"""HTTP surface of the files module. Thin: parse → use case → map to schema → envelope.

Rate-limit order: IP first, then auth, then DEFAULT, then permission, then the WRITE or UPLOAD
tier on mutations. Access rule: the owner, or a user holding the matching
`files:*` permission. Everyone else gets NotFound (anti-IDOR).
"""

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Header, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.celery_app import dispatch_task
from app.core.config import Settings, get_settings
from app.core.context import get_request_context
from app.core.db import get_db_session
from app.core.di import (
    get_audit_log_repository,
    get_current_user,
    get_user_role_repository,
    record_audit_event,
    require_permission,
)
from app.core.errors import error_responses
from app.core.exceptions import RateLimitedError
from app.core.rate_limit import RateLimitTier, rate_limit
from app.core.response import SuccessEnvelope, success_envelope
from app.modules.audit_log.domain.ports import AuditLogRepositoryPort
from app.modules.auth.domain.exceptions import InvalidTokenError
from app.modules.files.application.use_cases import (
    AbortUploadUseCase,
    ChunkReceipt,
    CompleteUploadUseCase,
    DeleteFileUseCase,
    GetFileUseCase,
    GetUploadStatusUseCase,
    InitiateUploadInput,
    InitiateUploadUseCase,
    ResolvePublicVariantUseCase,
    UploadChunkUseCase,
    UploadSingleFileUseCase,
)
from app.modules.files.domain.entities import UploadSession
from app.modules.files.domain.exceptions import (
    ChecksumMismatchError,
    ChunkConflictError,
    ChunkIndexError,
    ChunkSizeMismatchError,
    ChunksMissingError,
    ChunkTooLargeError,
    FileAssetNotFoundError,
    FileNotAvailableError,
    InvalidUploadError,
    SingleUploadTooLargeError,
    TooManyActiveUploadsError,
    UploadExpiredError,
    UploadNotFoundError,
    UploadStateError,
    UploadTooLargeError,
)
from app.modules.files.domain.ports import ObjectStoragePort
from app.modules.files.domain.value_objects import Region, Visibility, file_key
from app.modules.files.infrastructure.factory import build_object_storage, build_upload_policy
from app.modules.files.infrastructure.repository import (
    ChunkRepository,
    FileRepository,
    UploadRepository,
)
from app.modules.files.presentation.responses import (
    content_disposition,
    file_payload,
    private_variant_response,
)
from app.modules.files.presentation.schemas import (
    ChunkReceiptResponse,
    CompleteUploadResponse,
    FileAssetResponse,
    InitiateUploadRequest,
    UploadSessionResponse,
)
from app.modules.roles.domain.exceptions import InsufficientPermissionError
from app.modules.roles.domain.ports import UserRoleRepositoryPort
from app.modules.users.domain.entities import User

router = APIRouter(
    prefix="/files",
    tags=["files"],
    dependencies=[
        Depends(rate_limit(RateLimitTier.IP)),  # first: counts requests auth rejects
        Depends(get_current_user),
        Depends(rate_limit(RateLimitTier.DEFAULT)),
    ],
)

# Anonymous: IP-keyed limits only. Only PUBLIC, AVAILABLE variants are served here.
public_router = APIRouter(
    prefix="/public/files",
    tags=["files"],
    dependencies=[
        Depends(rate_limit(RateLimitTier.IP)),
        Depends(rate_limit(RateLimitTier.DEFAULT)),
    ],
)

_BLOCK = 1024 * 1024
_PUBLIC_CACHE_CONTROL = "public, max-age=86400"  # a deleted file may stay cached for up to a day

_UPLOAD_ERRORS = (
    InvalidTokenError,
    InsufficientPermissionError,
    RateLimitedError,
    UploadNotFoundError,
    UploadExpiredError,
    UploadStateError,
    ChunkIndexError,
    ChunkSizeMismatchError,
    ChunkTooLargeError,
    ChunkConflictError,
    ChecksumMismatchError,
    ChunksMissingError,
    UploadTooLargeError,
    InvalidUploadError,
    TooManyActiveUploadsError,
)


def get_object_storage(settings: Settings = Depends(get_settings)) -> ObjectStoragePort:
    return build_object_storage(settings)


async def _has_permission(user: User, permission: str, user_roles: UserRoleRepositoryPort) -> bool:
    roles = await user_roles.list_roles_for_user(user.id)
    return any(role.has_permission(permission) for role in roles)


def _session_payload(session: UploadSession, received: list[int] | None = None) -> dict[str, Any]:
    return UploadSessionResponse(
        upload_id=session.id,
        status=session.status.value,
        visibility=session.visibility,
        declared_size=session.declared_size,
        chunk_size=session.chunk_size,
        total_chunks=session.total_chunks,
        received_chunks=received or [],
        file_id=session.file_id,
        expires_at=session.expires_at,
    ).model_dump(by_alias=True)


def _parse_content_length(raw: str | None) -> int | None:
    """Content-Length is a fast-reject hint only; the streamed byte count is what's enforced."""
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


async def _read_blocks(file: UploadFile) -> AsyncIterator[bytes]:
    while block := await file.read(_BLOCK):
        yield block


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=SuccessEnvelope[CompleteUploadResponse],
    dependencies=[
        Depends(rate_limit(RateLimitTier.WRITE)),
        Depends(require_permission("files:write")),
    ],
    responses=error_responses(*_UPLOAD_ERRORS, SingleUploadTooLargeError, validation=True),
    summary="Upload a file in one request (up to one chunk)",
    description=(
        "Multipart form upload for files up to `UPLOAD_CHUNK_MAX_BYTES`. The file is stored as a "
        "single chunk, completed and handed to processing, all in this request. Larger files use "
        "`POST /files/uploads` followed by the chunk endpoints."
    ),
)
async def upload_file(
    # format: binary is what Swagger UI needs to render a file picker for multipart.
    file: UploadFile = File(json_schema_extra={"format": "binary"}),
    visibility: Visibility = Form(default=Visibility.PRIVATE),
    # Swagger pre-fills a form field with its example, so an empty example keeps "leave it blank"
    # working. An empty value means no checksum.
    sha256: str | None = Form(
        default=None,
        pattern=r"^([A-Fa-f0-9]{64})?$",
        json_schema_extra={"example": ""},
    ),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session, scope="function"),
    storage: ObjectStoragePort = Depends(get_object_storage),
    settings: Settings = Depends(get_settings),
    audit_logs: AuditLogRepositoryPort = Depends(get_audit_log_repository),
) -> dict[str, Any]:
    ctx = get_request_context()
    assert ctx.tenant_id is not None  # guaranteed by get_current_user
    size = file.size or 0
    upload_id, file_id, newly_completed = await UploadSingleFileUseCase(
        UploadRepository(session),
        ChunkRepository(session),
        FileRepository(session),
        storage,
        build_upload_policy(settings),
    ).execute(
        filename=file.filename or "file",
        size=size,
        sha256=sha256,
        visibility=visibility,
        body=_read_blocks(file),
        tenant_id=ctx.tenant_id,
        owner_id=user.id,
    )
    if newly_completed:
        # Dispatched once per upload; the worker retries until the file row is committed.
        await dispatch_task("files.process_upload", upload_id=str(upload_id), file_id=str(file_id))
        await record_audit_event(
            audit_logs,
            action="file.uploaded",
            resource_type="file",
            resource_id=str(file_id),
            after={"uploadId": str(upload_id), "visibility": visibility.value, "size": size},
        )
    payload = CompleteUploadResponse(
        upload_id=upload_id, file_id=file_id, status="UPLOADED"
    ).model_dump(by_alias=True)
    return success_envelope(payload, request_id=ctx.request_id)


@router.post(
    "/uploads",
    status_code=status.HTTP_201_CREATED,
    response_model=SuccessEnvelope[UploadSessionResponse],
    dependencies=[
        Depends(rate_limit(RateLimitTier.WRITE)),
        Depends(require_permission("files:write")),
    ],
    responses=error_responses(*_UPLOAD_ERRORS, validation=True),
)
async def initiate_upload(
    body: InitiateUploadRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session, scope="function"),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    ctx = get_request_context()
    assert ctx.tenant_id is not None  # guaranteed by get_current_user
    upload = await InitiateUploadUseCase(
        UploadRepository(session), build_upload_policy(settings)
    ).execute(
        InitiateUploadInput(
            filename=body.filename,
            size=body.size,
            chunk_size=body.chunk_size,
            sha256=body.sha256,
            visibility=body.visibility,
        ),
        tenant_id=ctx.tenant_id,
        owner_id=user.id,
    )
    return success_envelope(_session_payload(upload), request_id=ctx.request_id)


@router.get(
    "/uploads/{upload_id}",
    response_model=SuccessEnvelope[UploadSessionResponse],
    dependencies=[Depends(require_permission("files:write"))],
    responses=error_responses(*_UPLOAD_ERRORS, validation=True),
)
async def get_upload_status(
    upload_id: UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session, scope="function"),
) -> dict[str, Any]:
    upload, received = await GetUploadStatusUseCase(
        UploadRepository(session), ChunkRepository(session)
    ).execute(upload_id, user.id)
    return success_envelope(
        _session_payload(upload, received), request_id=get_request_context().request_id
    )


@router.put(
    "/uploads/{upload_id}/chunks/{index}",
    response_model=SuccessEnvelope[ChunkReceiptResponse],
    dependencies=[
        Depends(rate_limit(RateLimitTier.UPLOAD)),
        Depends(require_permission("files:write")),
    ],
    responses=error_responses(*_UPLOAD_ERRORS, validation=True),
    # The body is read as a raw stream (not parsed by FastAPI), so it has no schema of its own.
    # Declaring it here gives /docs and /redoc a file picker; the server still streams the bytes.
    openapi_extra={
        "requestBody": {
            "required": True,
            "description": "The raw bytes of this chunk (at most the declared chunk size).",
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            },
        }
    },
)
async def upload_chunk(
    upload_id: UUID,
    index: int,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session, scope="function"),
    storage: ObjectStoragePort = Depends(get_object_storage),
    x_chunk_sha256: str | None = Header(default=None, alias="X-Chunk-SHA256"),
) -> dict[str, Any]:
    receipt: ChunkReceipt = await UploadChunkUseCase(
        UploadRepository(session), ChunkRepository(session), storage
    ).execute(
        upload_id=upload_id,
        owner_id=user.id,
        index=index,
        body=request.stream(),
        declared_sha256=x_chunk_sha256,
        content_length=_parse_content_length(request.headers.get("content-length")),
    )
    payload = ChunkReceiptResponse(
        index=receipt.index,
        size_bytes=receipt.size_bytes,
        sha256=receipt.sha256,
        duplicate=receipt.duplicate,
    ).model_dump(by_alias=True)
    return success_envelope(payload, request_id=get_request_context().request_id)


@router.post(
    "/uploads/{upload_id}/complete",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=SuccessEnvelope[CompleteUploadResponse],
    dependencies=[
        Depends(rate_limit(RateLimitTier.WRITE)),
        Depends(require_permission("files:write")),
    ],
    responses=error_responses(*_UPLOAD_ERRORS, validation=True),
)
async def complete_upload(
    upload_id: UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session, scope="function"),
    audit_logs: AuditLogRepositoryPort = Depends(get_audit_log_repository),
) -> dict[str, Any]:
    ctx = get_request_context()
    upload, file_id, newly_completed = await CompleteUploadUseCase(
        UploadRepository(session), ChunkRepository(session), FileRepository(session)
    ).execute(upload_id, user.id)
    if newly_completed:
        # Dispatched once per upload; the worker retries until the file row is committed.
        await dispatch_task("files.process_upload", upload_id=str(upload_id), file_id=str(file_id))
        await record_audit_event(
            audit_logs,
            action="file.uploaded",
            resource_type="file",
            resource_id=str(file_id),
            after={
                "uploadId": str(upload_id),
                "visibility": upload.visibility.value,
                "size": upload.declared_size,
            },
        )
    payload = CompleteUploadResponse(
        upload_id=upload_id, file_id=file_id, status="UPLOADED"
    ).model_dump(by_alias=True)
    return success_envelope(payload, request_id=ctx.request_id)


@router.delete(
    "/uploads/{upload_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[
        Depends(rate_limit(RateLimitTier.WRITE)),
        Depends(require_permission("files:write")),
    ],
    responses=error_responses(*_UPLOAD_ERRORS, validation=True),
)
async def abort_upload(
    upload_id: UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session, scope="function"),
    storage: ObjectStoragePort = Depends(get_object_storage),
    audit_logs: AuditLogRepositoryPort = Depends(get_audit_log_repository),
) -> None:
    aborted = await AbortUploadUseCase(UploadRepository(session), storage).execute(
        upload_id, user.id
    )
    if aborted:
        await record_audit_event(
            audit_logs,
            action="file.upload_aborted",
            resource_type="file_upload",
            resource_id=str(upload_id),
        )


@router.get(
    "/{file_id}",
    response_model=SuccessEnvelope[FileAssetResponse],
    responses=error_responses(
        InvalidTokenError, RateLimitedError, FileAssetNotFoundError, validation=True
    ),
)
async def get_file(
    file_id: UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session, scope="function"),
    user_roles: UserRoleRepositoryPort = Depends(get_user_role_repository),
) -> dict[str, Any]:
    can_read_all = await _has_permission(user, "files:read", user_roles)
    asset, variants = await GetFileUseCase(FileRepository(session)).execute(
        file_id, user.id, can_read_all=can_read_all
    )
    return success_envelope(
        file_payload(asset, variants), request_id=get_request_context().request_id
    )


@router.get(
    "/{file_id}/content",
    response_class=StreamingResponse,
    responses=error_responses(
        InvalidTokenError,
        RateLimitedError,
        FileAssetNotFoundError,
        FileNotAvailableError,
        validation=True,
    ),
)
async def get_file_content(
    file_id: UUID,
    variant: str = "original",
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session, scope="function"),
    user_roles: UserRoleRepositoryPort = Depends(get_user_role_repository),
    storage: ObjectStoragePort = Depends(get_object_storage),
) -> StreamingResponse:
    can_read_all = await _has_permission(user, "files:read", user_roles)
    asset, chosen = await GetFileUseCase(FileRepository(session)).resolve_variant(
        file_id, user.id, can_read_all=can_read_all, variant_name=variant
    )
    return private_variant_response(storage, asset, chosen)


@router.delete(
    "/{file_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(rate_limit(RateLimitTier.WRITE))],
    responses=error_responses(
        InvalidTokenError,
        RateLimitedError,
        FileAssetNotFoundError,
        FileNotAvailableError,
        validation=True,
    ),
)
async def delete_file(
    file_id: UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session, scope="function"),
    user_roles: UserRoleRepositoryPort = Depends(get_user_role_repository),
    audit_logs: AuditLogRepositoryPort = Depends(get_audit_log_repository),
) -> None:
    can_delete_all = await _has_permission(user, "files:delete", user_roles)
    await DeleteFileUseCase(FileRepository(session)).execute(
        file_id, user.id, can_delete_all=can_delete_all
    )
    await record_audit_event(
        audit_logs, action="file.deleted", resource_type="file", resource_id=str(file_id)
    )


@public_router.get(
    "/{file_id}/{variant}",
    responses=error_responses(FileAssetNotFoundError, RateLimitedError, validation=True),
)
async def get_public_file(
    file_id: UUID,
    variant: str,
    session: AsyncSession = Depends(get_db_session, scope="function"),
    storage: ObjectStoragePort = Depends(get_object_storage),
) -> StreamingResponse:
    asset, chosen = await ResolvePublicVariantUseCase(FileRepository(session)).execute(
        file_id, variant
    )
    return StreamingResponse(
        storage.read(file_key(Region.PUBLIC, asset.id, "variants", chosen.name)),
        media_type=chosen.mime_type,
        headers={
            "Content-Disposition": content_disposition(asset),
            "Content-Length": str(chosen.size_bytes),
            "Cache-Control": _PUBLIC_CACHE_CONTROL,
            "ETag": f'"{chosen.sha256}"',
        },
    )
