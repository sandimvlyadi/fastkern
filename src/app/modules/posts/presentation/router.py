"""Posts: an external-API CRUD demo. Every endpoint proxies to JSONPlaceholder's `/posts`
instead of a local database table. See docs/module-guide.md."""

from typing import Any

from fastapi import APIRouter, Depends, status

from app.core.context import get_request_context
from app.core.di import (
    get_audit_log_repository,
    get_current_user,
    get_posts_api_port,
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
from app.modules.posts.application.dto import CreatePostInput, UpdatePostInput
from app.modules.posts.application.use_cases import (
    CreatePostUseCase,
    DeletePostUseCase,
    GetPostUseCase,
    ListPostsUseCase,
    UpdatePostUseCase,
)
from app.modules.posts.domain.exceptions import PostNotFoundError, PostsServiceError
from app.modules.posts.domain.ports import PostsApiPort
from app.modules.posts.presentation.schemas import (
    CreatePostRequest,
    PostResponse,
    UpdatePostRequest,
)
from app.modules.roles.domain.exceptions import InsufficientPermissionError

router = APIRouter(
    prefix="/posts",
    tags=["posts"],
    dependencies=[
        Depends(rate_limit(RateLimitTier.IP)),  # first: counts requests auth rejects
        Depends(get_current_user),
        Depends(rate_limit(RateLimitTier.DEFAULT)),
    ],
)


@router.get(
    "",
    response_model=SuccessEnvelope[list[PostResponse]],
    dependencies=[Depends(require_permission("posts:read"))],
    responses=error_responses(
        InvalidTokenError,
        InsufficientPermissionError,
        RateLimitedError,
        PostsServiceError,
        validation=True,
    ),
)
async def list_posts(
    params: OffsetParams = Depends(), posts_api: PostsApiPort = Depends(get_posts_api_port)
) -> dict[str, Any]:
    result = await ListPostsUseCase(posts_api).execute(params)
    items = [PostResponse.model_validate(p).model_dump(by_alias=True) for p in result.items]
    return success_envelope(
        items, request_id=get_request_context().request_id, pagination=result.to_meta()
    )


@router.get(
    "/{post_id}",
    response_model=SuccessEnvelope[PostResponse],
    dependencies=[Depends(require_permission("posts:read"))],
    responses=error_responses(
        InvalidTokenError,
        InsufficientPermissionError,
        RateLimitedError,
        PostNotFoundError,
        PostsServiceError,
        validation=True,
    ),
)
async def get_post(
    post_id: int, posts_api: PostsApiPort = Depends(get_posts_api_port)
) -> dict[str, Any]:
    post = await GetPostUseCase(posts_api).execute(post_id)
    payload = PostResponse.model_validate(post).model_dump(by_alias=True)
    return success_envelope(payload, request_id=get_request_context().request_id)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=SuccessEnvelope[PostResponse],
    dependencies=[
        Depends(rate_limit(RateLimitTier.WRITE)),
        Depends(require_permission("posts:write")),
    ],
    responses=error_responses(
        InvalidTokenError,
        InsufficientPermissionError,
        RateLimitedError,
        PostsServiceError,
        validation=True,
    ),
)
async def create_post(
    body: CreatePostRequest,
    posts_api: PostsApiPort = Depends(get_posts_api_port),
    audit_logs: AuditLogRepositoryPort = Depends(get_audit_log_repository),
) -> dict[str, Any]:
    ctx = get_request_context()
    post = await CreatePostUseCase(posts_api).execute(
        CreatePostInput(user_id=body.user_id, title=body.title, body=body.body)
    )
    await record_audit_event(
        audit_logs,
        action="post.created",
        resource_type="post",
        resource_id=str(post.id),
        after=post.audit_snapshot(),
    )
    payload = PostResponse.model_validate(post).model_dump(by_alias=True)
    return success_envelope(payload, request_id=ctx.request_id)


@router.patch(
    "/{post_id}",
    response_model=SuccessEnvelope[PostResponse],
    dependencies=[
        Depends(rate_limit(RateLimitTier.WRITE)),
        Depends(require_permission("posts:write")),
    ],
    responses=error_responses(
        InvalidTokenError,
        InsufficientPermissionError,
        RateLimitedError,
        PostNotFoundError,
        PostsServiceError,
        validation=True,
    ),
)
async def update_post(
    post_id: int,
    body: UpdatePostRequest,
    posts_api: PostsApiPort = Depends(get_posts_api_port),
    audit_logs: AuditLogRepositoryPort = Depends(get_audit_log_repository),
) -> dict[str, Any]:
    before, post = await UpdatePostUseCase(posts_api).execute(
        post_id, UpdatePostInput(**body.model_dump(exclude_unset=True))
    )
    if before.audit_snapshot() != post.audit_snapshot():  # an empty/no-op PATCH audits nothing
        await record_audit_event(
            audit_logs,
            action="post.updated",
            resource_type="post",
            resource_id=str(post_id),
            before=before.audit_snapshot(),
            after=post.audit_snapshot(),
        )
    payload = PostResponse.model_validate(post).model_dump(by_alias=True)
    return success_envelope(payload, request_id=get_request_context().request_id)


@router.delete(
    "/{post_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[
        Depends(rate_limit(RateLimitTier.WRITE)),
        Depends(require_permission("posts:delete")),
    ],
    responses=error_responses(
        InvalidTokenError,
        InsufficientPermissionError,
        RateLimitedError,
        PostNotFoundError,
        PostsServiceError,
        validation=True,
    ),
)
async def delete_post(
    post_id: int,
    posts_api: PostsApiPort = Depends(get_posts_api_port),
    audit_logs: AuditLogRepositoryPort = Depends(get_audit_log_repository),
) -> None:
    before = await DeletePostUseCase(posts_api).execute(post_id)
    await record_audit_event(
        audit_logs,
        action="post.deleted",
        resource_type="post",
        resource_id=str(post_id),
        before=before.audit_snapshot(),
    )
