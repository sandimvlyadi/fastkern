from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, UploadFile, status

from app.core.celery_app import dispatch_task
from app.core.config import Settings, get_settings
from app.core.context import get_request_context
from app.core.di import (
    get_audit_log_repository,
    get_chunk_repository,
    get_current_user,
    get_file_repository,
    get_object_storage,
    get_token_session_store,
    get_upload_policy,
    get_upload_repository,
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
from app.modules.auth.domain.ports import TokenSessionStorePort
from app.modules.files.application.use_cases import UploadPolicy
from app.modules.files.domain.exceptions import (
    ChunkSizeMismatchError,
    ChunkTooLargeError,
    InvalidUploadError,
    TooManyActiveUploadsError,
)
from app.modules.files.domain.ports import (
    ChunkRepositoryPort,
    FileRepositoryPort,
    ObjectStoragePort,
    UploadRepositoryPort,
)
from app.modules.roles.domain.exceptions import InsufficientPermissionError
from app.modules.users.application.dto import (
    ChangePasswordInput,
    UpdateAvatarInput,
    UpdateUserInput,
)
from app.modules.users.application.use_cases import (
    ActivateUserUseCase,
    ChangeOwnPasswordUseCase,
    DeactivateUserUseCase,
    DeleteUserUseCase,
    GetUserUseCase,
    ListUsersUseCase,
    RemoveAvatarUseCase,
    UpdateAvatarUseCase,
    UpdateUserUseCase,
)
from app.modules.users.domain.entities import User
from app.modules.users.domain.exceptions import (
    AvatarChangeInProgressError,
    AvatarMustBeImageError,
    AvatarTooLargeError,
    CannotModifySelfError,
    IncorrectCurrentPasswordError,
    UserHasNoAvatarError,
    UserNotFoundError,
)
from app.modules.users.domain.ports import UserRepositoryPort
from app.modules.users.presentation.schemas import (
    AvatarResponse,
    ChangePasswordRequest,
    UpdateUserRequest,
    UserResponse,
    avatar_url,
)

router = APIRouter(
    prefix="/users",
    tags=["users"],
    dependencies=[
        Depends(rate_limit(RateLimitTier.IP)),
        Depends(get_current_user),
        Depends(rate_limit(RateLimitTier.DEFAULT)),
    ],
)

_ALLOWED_AVATAR_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
_BLOCK = 1024 * 1024
_AVATAR_ERRORS = (
    InvalidTokenError,
    RateLimitedError,
    AvatarMustBeImageError,
    AvatarTooLargeError,
    AvatarChangeInProgressError,
    InvalidUploadError,
    # Reachable via the reused files-module upload chain (InitiateUploadUseCase/
    # UploadChunkUseCase), not just the avatar-specific checks above.
    TooManyActiveUploadsError,
    ChunkSizeMismatchError,
    ChunkTooLargeError,
)


def _avatar_payload(user: User) -> dict[str, Any]:
    return AvatarResponse(
        avatar_file_id=user.avatar_file_id, avatar_url=avatar_url(user.avatar_file_id)
    ).model_dump(by_alias=True)


async def _read_blocks(file: UploadFile) -> AsyncIterator[bytes]:
    while block := await file.read(_BLOCK):
        yield block


def _check_avatar_content_type(file: UploadFile) -> None:
    # Fast client-declared-metadata check only (not authoritative — the files pipeline's
    # content-signature detection in the worker is what actually decides `kind`; see
    # `users.finalize_avatar` for the authoritative, async rejection path). MIME types are
    # case-insensitive and may carry parameters (e.g. "image/PNG" or "image/png; charset=binary"),
    # so normalize before comparing.
    content_type = (file.content_type or "").split(";", 1)[0].strip().lower()
    if content_type not in _ALLOWED_AVATAR_TYPES:
        raise AvatarMustBeImageError()


async def _upload_avatar(
    *,
    target_user_id: UUID,
    caller_id: UUID,
    file: UploadFile,
    storage: ObjectStoragePort,
    settings: Settings,
    policy: UploadPolicy,
    audit_logs: AuditLogRepositoryPort,
    users: UserRepositoryPort,
    uploads: UploadRepositoryPort,
    chunks: ChunkRepositoryPort,
    files: FileRepositoryPort,
) -> dict[str, Any]:
    _check_avatar_content_type(file)
    size = file.size or 0
    if size > settings.avatar_max_bytes:
        raise AvatarTooLargeError(max_bytes=settings.avatar_max_bytes)
    ctx = get_request_context()
    assert ctx.tenant_id is not None  # guaranteed by get_current_user
    user, upload_id, file_id = await UpdateAvatarUseCase(
        users, uploads, chunks, files, storage, policy
    ).execute(
        UpdateAvatarInput(
            target_user_id=target_user_id,
            caller_id=caller_id,
            tenant_id=ctx.tenant_id,
            filename=file.filename or "avatar",
            size=size,
            sha256=None,
            body=_read_blocks(file),
        )
    )
    # Dispatched every time: the generic pipeline assembles/scans/renders the image, and the
    # avatar-specific follow-up undoes the swap if it doesn't turn out to be a usable image.
    await dispatch_task("files.process_upload", upload_id=str(upload_id), file_id=str(file_id))
    await dispatch_task("users.finalize_avatar", user_id=str(target_user_id), file_id=str(file_id))
    await record_audit_event(
        audit_logs,
        action="user.avatar_updated",
        resource_type="user",
        resource_id=str(target_user_id),
        after={"fileId": str(file_id)},
    )
    return success_envelope(_avatar_payload(user), request_id=ctx.request_id)


@router.get(
    "",
    response_model=SuccessEnvelope[list[UserResponse]],
    responses=error_responses(
        InvalidTokenError, InsufficientPermissionError, RateLimitedError, validation=True
    ),
)
async def list_users(
    params: OffsetParams = Depends(),
    users: UserRepositoryPort = Depends(get_user_repository),
    _: object = Depends(require_permission("users:read")),
) -> dict[str, Any]:
    result = await ListUsersUseCase(users).execute(params)
    items = []
    for u in result.items:
        item = UserResponse.model_validate(u).model_dump(by_alias=True)
        item["avatarUrl"] = avatar_url(u.avatar_file_id)
        items.append(item)
    return success_envelope(
        items, request_id=get_request_context().request_id, pagination=result.to_meta()
    )


@router.get(
    "/{user_id}",
    response_model=SuccessEnvelope[UserResponse],
    responses=error_responses(
        InvalidTokenError,
        InsufficientPermissionError,
        RateLimitedError,
        UserNotFoundError,
        validation=True,
    ),
)
async def get_user(
    user_id: UUID,
    users: UserRepositoryPort = Depends(get_user_repository),
    _: object = Depends(require_permission("users:read")),
) -> dict[str, Any]:
    user = await GetUserUseCase(users).execute(user_id)
    payload = UserResponse.model_validate(user).model_dump(by_alias=True)
    payload["avatarUrl"] = avatar_url(user.avatar_file_id)
    return success_envelope(payload, request_id=get_request_context().request_id)


@router.patch(
    "/{user_id}",
    response_model=SuccessEnvelope[UserResponse],
    dependencies=[Depends(rate_limit(RateLimitTier.WRITE))],
    responses=error_responses(
        InvalidTokenError,
        InsufficientPermissionError,
        RateLimitedError,
        UserNotFoundError,
        validation=True,
    ),
)
async def update_user(
    user_id: UUID,
    body: UpdateUserRequest,
    users: UserRepositoryPort = Depends(get_user_repository),
    sessions: TokenSessionStorePort = Depends(get_token_session_store),
    audit_logs: AuditLogRepositoryPort = Depends(get_audit_log_repository),
    _: object = Depends(require_permission("users:write")),
) -> dict[str, Any]:
    before, user = await UpdateUserUseCase(users, sessions).execute(
        user_id,
        UpdateUserInput(
            full_name=body.full_name,
            locale=body.locale,
            timezone=body.timezone,
            new_password=body.password,
        ),
    )
    await record_audit_event(
        audit_logs,
        action="user.updated",
        resource_type="user",
        resource_id=str(user_id),
        before={"fullName": before.full_name, "locale": before.locale, "timezone": before.timezone},
        after={"fullName": user.full_name, "locale": user.locale, "timezone": user.timezone},
    )
    if body.password is not None:
        # A dedicated action (not folded into "user.updated") so a password reset can be
        # filtered/alerted on by action alone, without inspecting every update's payload.
        # No before/after: there is nothing about a password change that's safe to log.
        await record_audit_event(
            audit_logs,
            action="user.password_reset_by_admin",
            resource_type="user",
            resource_id=str(user_id),
        )
    payload = UserResponse.model_validate(user).model_dump(by_alias=True)
    payload["avatarUrl"] = avatar_url(user.avatar_file_id)
    return success_envelope(payload, request_id=get_request_context().request_id)


@router.post(
    "/me/password",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(rate_limit(RateLimitTier.WRITE))],
    responses=error_responses(
        InvalidTokenError,
        IncorrectCurrentPasswordError,
        RateLimitedError,
        validation=True,
    ),
)
async def change_my_password(
    body: ChangePasswordRequest,
    users: UserRepositoryPort = Depends(get_user_repository),
    sessions: TokenSessionStorePort = Depends(get_token_session_store),
    audit_logs: AuditLogRepositoryPort = Depends(get_audit_log_repository),
    caller: User = Depends(get_current_user),
) -> None:
    await ChangeOwnPasswordUseCase(users, sessions).execute(
        caller.id,
        ChangePasswordInput(current_password=body.current_password, new_password=body.new_password),
    )
    await record_audit_event(
        audit_logs,
        action="user.password_changed",
        resource_type="user",
        resource_id=str(caller.id),
    )


@router.post(
    "/{user_id}/deactivate",
    response_model=SuccessEnvelope[UserResponse],
    dependencies=[Depends(rate_limit(RateLimitTier.WRITE))],
    responses=error_responses(
        InvalidTokenError,
        InsufficientPermissionError,
        RateLimitedError,
        UserNotFoundError,
        CannotModifySelfError,
        validation=True,
    ),
)
async def deactivate_user(
    user_id: UUID,
    users: UserRepositoryPort = Depends(get_user_repository),
    audit_logs: AuditLogRepositoryPort = Depends(get_audit_log_repository),
    caller: User = Depends(require_permission("users:write")),
) -> dict[str, Any]:
    before, user = await DeactivateUserUseCase(users).execute(user_id, caller.id)
    await record_audit_event(
        audit_logs,
        action="user.deactivated",
        resource_type="user",
        resource_id=str(user_id),
        before=before.audit_snapshot(),
        after=user.audit_snapshot(),
    )
    payload = UserResponse.model_validate(user).model_dump(by_alias=True)
    payload["avatarUrl"] = avatar_url(user.avatar_file_id)
    return success_envelope(payload, request_id=get_request_context().request_id)


@router.post(
    "/{user_id}/activate",
    response_model=SuccessEnvelope[UserResponse],
    dependencies=[Depends(rate_limit(RateLimitTier.WRITE))],
    responses=error_responses(
        InvalidTokenError,
        InsufficientPermissionError,
        RateLimitedError,
        UserNotFoundError,
        validation=True,
    ),
)
async def activate_user(
    user_id: UUID,
    users: UserRepositoryPort = Depends(get_user_repository),
    audit_logs: AuditLogRepositoryPort = Depends(get_audit_log_repository),
    _: object = Depends(require_permission("users:write")),
) -> dict[str, Any]:
    before, user = await ActivateUserUseCase(users).execute(user_id)
    await record_audit_event(
        audit_logs,
        action="user.activated",
        resource_type="user",
        resource_id=str(user_id),
        before=before.audit_snapshot(),
        after=user.audit_snapshot(),
    )
    payload = UserResponse.model_validate(user).model_dump(by_alias=True)
    payload["avatarUrl"] = avatar_url(user.avatar_file_id)
    return success_envelope(payload, request_id=get_request_context().request_id)


@router.delete(
    "/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(rate_limit(RateLimitTier.WRITE))],
    responses=error_responses(
        InvalidTokenError,
        InsufficientPermissionError,
        RateLimitedError,
        UserNotFoundError,
        CannotModifySelfError,
        validation=True,
    ),
)
async def delete_user(
    user_id: UUID,
    users: UserRepositoryPort = Depends(get_user_repository),
    audit_logs: AuditLogRepositoryPort = Depends(get_audit_log_repository),
    caller: User = Depends(require_permission("users:delete")),
) -> None:
    before = await DeleteUserUseCase(users).execute(user_id, caller.id)
    await record_audit_event(
        audit_logs,
        action="user.deleted",
        resource_type="user",
        resource_id=str(user_id),
        before=before.audit_snapshot(),
    )


@router.put(
    "/me/avatar",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=SuccessEnvelope[AvatarResponse],
    dependencies=[Depends(rate_limit(RateLimitTier.WRITE))],
    responses=error_responses(*_AVATAR_ERRORS, validation=True),
)
async def update_my_avatar(
    file: UploadFile = File(json_schema_extra={"format": "binary"}),
    caller: User = Depends(get_current_user),
    storage: ObjectStoragePort = Depends(get_object_storage),
    settings: Settings = Depends(get_settings),
    policy: UploadPolicy = Depends(get_upload_policy),
    audit_logs: AuditLogRepositoryPort = Depends(get_audit_log_repository),
    users: UserRepositoryPort = Depends(get_user_repository),
    uploads: UploadRepositoryPort = Depends(get_upload_repository),
    chunks: ChunkRepositoryPort = Depends(get_chunk_repository),
    files: FileRepositoryPort = Depends(get_file_repository),
) -> dict[str, Any]:
    return await _upload_avatar(
        target_user_id=caller.id,
        caller_id=caller.id,
        file=file,
        storage=storage,
        settings=settings,
        policy=policy,
        audit_logs=audit_logs,
        users=users,
        uploads=uploads,
        chunks=chunks,
        files=files,
    )


@router.put(
    "/{user_id}/avatar",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=SuccessEnvelope[AvatarResponse],
    dependencies=[Depends(rate_limit(RateLimitTier.WRITE))],
    responses=error_responses(
        *_AVATAR_ERRORS, InsufficientPermissionError, UserNotFoundError, validation=True
    ),
)
async def update_user_avatar(
    user_id: UUID,
    file: UploadFile = File(json_schema_extra={"format": "binary"}),
    storage: ObjectStoragePort = Depends(get_object_storage),
    settings: Settings = Depends(get_settings),
    policy: UploadPolicy = Depends(get_upload_policy),
    audit_logs: AuditLogRepositoryPort = Depends(get_audit_log_repository),
    users: UserRepositoryPort = Depends(get_user_repository),
    uploads: UploadRepositoryPort = Depends(get_upload_repository),
    chunks: ChunkRepositoryPort = Depends(get_chunk_repository),
    files: FileRepositoryPort = Depends(get_file_repository),
    caller: User = Depends(require_permission("users:write")),
) -> dict[str, Any]:
    return await _upload_avatar(
        target_user_id=user_id,
        caller_id=caller.id,
        file=file,
        storage=storage,
        settings=settings,
        policy=policy,
        audit_logs=audit_logs,
        users=users,
        uploads=uploads,
        chunks=chunks,
        files=files,
    )


@router.delete(
    "/me/avatar",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(rate_limit(RateLimitTier.WRITE))],
    responses=error_responses(InvalidTokenError, RateLimitedError, UserHasNoAvatarError),
)
async def remove_my_avatar(
    caller: User = Depends(get_current_user),
    users: UserRepositoryPort = Depends(get_user_repository),
    files: FileRepositoryPort = Depends(get_file_repository),
    audit_logs: AuditLogRepositoryPort = Depends(get_audit_log_repository),
) -> None:
    await RemoveAvatarUseCase(users, files).execute(caller.id, caller.id)
    await record_audit_event(
        audit_logs,
        action="user.avatar_removed",
        resource_type="user",
        resource_id=str(caller.id),
    )


@router.delete(
    "/{user_id}/avatar",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(rate_limit(RateLimitTier.WRITE))],
    responses=error_responses(
        InvalidTokenError,
        InsufficientPermissionError,
        RateLimitedError,
        UserNotFoundError,
        UserHasNoAvatarError,
    ),
)
async def remove_user_avatar(
    user_id: UUID,
    users: UserRepositoryPort = Depends(get_user_repository),
    files: FileRepositoryPort = Depends(get_file_repository),
    audit_logs: AuditLogRepositoryPort = Depends(get_audit_log_repository),
    caller: User = Depends(require_permission("users:write")),
) -> None:
    await RemoveAvatarUseCase(users, files).execute(user_id, caller.id)
    await record_audit_event(
        audit_logs,
        action="user.avatar_removed",
        resource_type="user",
        resource_id=str(user_id),
    )
