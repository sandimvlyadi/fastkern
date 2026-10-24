"""Partner (B2B) facing API surface — API-key-authenticated via `require_scope`,
never JWT/RBAC: a partner has no password, session, or RBAC role, only a
long-lived `X-API-Key` and a fixed set of scopes.

Deliberately presentation-only: `partners` has no domain/application/
infrastructure layers of its own. It reuses existing modules' use cases and
response schemas through the same `core/di.py` providers those modules' own
routers use — only the authentication/authorization/rate-limit dependency
chain differs from the human-facing equivalent. Every endpoint here is
read-only and checks its own `<resource>:read` scope, so a key only reaches
the resources it was explicitly granted. This keeps the partner-facing
OpenAPI surface small and intentional instead of growing implicitly with
every internal endpoint.

Tenant isolation needs nothing extra: `get_current_api_client` scopes the
request to the key's tenant, which every repository query below is filtered by."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.core.context import get_request_context
from app.core.di import (
    get_current_api_client,
    get_example_repository,
    get_file_repository,
    get_master_example_repository,
    get_object_storage,
    get_posts_api_port,
    get_role_repository,
    get_user_repository,
    get_user_role_repository,
    require_scope,
)
from app.core.errors import error_responses
from app.core.exceptions import RateLimitedError
from app.core.pagination import OffsetParams
from app.core.rate_limit import RateLimitTier, rate_limit
from app.core.response import SuccessEnvelope, success_envelope
from app.modules.api_clients.domain.entities import ApiKey
from app.modules.api_clients.domain.exceptions import InvalidApiKeyError, ScopeDeniedError
from app.modules.examples.application.use_cases import GetExampleUseCase, ListExamplesUseCase
from app.modules.examples.domain.exceptions import ExampleNotFoundError
from app.modules.examples.domain.ports import ExampleRepositoryPort
from app.modules.examples.presentation.schemas import ExampleResponse
from app.modules.files.application.use_cases import GetFileUseCase
from app.modules.files.domain.exceptions import FileAssetNotFoundError, FileNotAvailableError
from app.modules.files.domain.ports import FileRepositoryPort, ObjectStoragePort
from app.modules.files.presentation.responses import file_payload, private_variant_response
from app.modules.files.presentation.schemas import FileAssetResponse
from app.modules.master_examples.application.use_cases import (
    GetMasterExampleUseCase,
    ListMasterExamplesUseCase,
)
from app.modules.master_examples.domain.exceptions import MasterExampleNotFoundError
from app.modules.master_examples.domain.ports import MasterExampleRepositoryPort
from app.modules.master_examples.presentation.schemas import MasterExampleResponse
from app.modules.posts.application.use_cases import GetPostUseCase, ListPostsUseCase
from app.modules.posts.domain.exceptions import PostNotFoundError, PostsServiceError
from app.modules.posts.domain.ports import PostsApiPort
from app.modules.posts.presentation.schemas import PostResponse
from app.modules.roles.application.use_cases import ListRolesUseCase, ListUserRolesUseCase
from app.modules.roles.domain.ports import RoleRepositoryPort, UserRoleRepositoryPort
from app.modules.roles.presentation.schemas import RoleResponse, UserRoleResponse
from app.modules.users.application.use_cases import GetUserUseCase, ListUsersUseCase
from app.modules.users.domain.exceptions import UserNotFoundError
from app.modules.users.domain.ports import UserRepositoryPort
from app.modules.users.presentation.schemas import UserResponse, avatar_url

router = APIRouter(
    prefix="/partners",
    tags=["partners"],
    dependencies=[
        Depends(rate_limit(RateLimitTier.IP)),  # first: counts requests auth rejects
        Depends(get_current_api_client),
        # Before each route's `require_scope`, so a scope-denied 403 still counts against the
        # quota.
        Depends(rate_limit(RateLimitTier.PARTNER)),
    ],
)

# Raised by the router-level dependencies, so every endpoint below documents them.
_AUTH_ERRORS = (InvalidApiKeyError, ScopeDeniedError, RateLimitedError)


@router.get(
    "/posts",
    response_model=SuccessEnvelope[list[PostResponse]],
    dependencies=[Depends(require_scope("posts:read"))],
    responses=error_responses(*_AUTH_ERRORS, PostsServiceError, validation=True),
)
async def list_partner_posts(
    params: OffsetParams = Depends(), posts_api: PostsApiPort = Depends(get_posts_api_port)
) -> dict[str, Any]:
    result = await ListPostsUseCase(posts_api).execute(params)
    items = [PostResponse.model_validate(p).model_dump(by_alias=True) for p in result.items]
    return success_envelope(
        items, request_id=get_request_context().request_id, pagination=result.to_meta()
    )


@router.get(
    "/posts/{post_id}",
    response_model=SuccessEnvelope[PostResponse],
    dependencies=[Depends(require_scope("posts:read"))],
    responses=error_responses(*_AUTH_ERRORS, PostNotFoundError, PostsServiceError, validation=True),
)
async def get_partner_post(
    post_id: int, posts_api: PostsApiPort = Depends(get_posts_api_port)
) -> dict[str, Any]:
    post = await GetPostUseCase(posts_api).execute(post_id)
    payload = PostResponse.model_validate(post).model_dump(by_alias=True)
    return success_envelope(payload, request_id=get_request_context().request_id)


@router.get(
    "/users",
    response_model=SuccessEnvelope[list[UserResponse]],
    dependencies=[Depends(require_scope("users:read"))],
    responses=error_responses(*_AUTH_ERRORS, validation=True),
)
async def list_partner_users(
    params: OffsetParams = Depends(), users: UserRepositoryPort = Depends(get_user_repository)
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
    "/users/{user_id}",
    response_model=SuccessEnvelope[UserResponse],
    dependencies=[Depends(require_scope("users:read"))],
    responses=error_responses(*_AUTH_ERRORS, UserNotFoundError, validation=True),
)
async def get_partner_user(
    user_id: UUID, users: UserRepositoryPort = Depends(get_user_repository)
) -> dict[str, Any]:
    user = await GetUserUseCase(users).execute(user_id)
    payload = UserResponse.model_validate(user).model_dump(by_alias=True)
    payload["avatarUrl"] = avatar_url(user.avatar_file_id)
    return success_envelope(payload, request_id=get_request_context().request_id)


@router.get(
    "/roles",
    response_model=SuccessEnvelope[list[RoleResponse]],
    dependencies=[Depends(require_scope("roles:read"))],
    responses=error_responses(*_AUTH_ERRORS, validation=True),
)
async def list_partner_roles(
    params: OffsetParams = Depends(), roles: RoleRepositoryPort = Depends(get_role_repository)
) -> dict[str, Any]:
    result = await ListRolesUseCase(roles).execute(params)
    items = [RoleResponse.model_validate(r).model_dump(by_alias=True) for r in result.items]
    return success_envelope(
        items, request_id=get_request_context().request_id, pagination=result.to_meta()
    )


@router.get(
    "/roles/users/{user_id}",
    response_model=SuccessEnvelope[list[UserRoleResponse]],
    dependencies=[Depends(require_scope("roles:read"))],
    responses=error_responses(*_AUTH_ERRORS, validation=True),
)
async def list_partner_user_roles(
    user_id: UUID,
    params: OffsetParams = Depends(),
    user_roles: UserRoleRepositoryPort = Depends(get_user_role_repository),
) -> dict[str, Any]:
    result = await ListUserRolesUseCase(user_roles).execute(user_id, params)
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


@router.get(
    "/master/examples",
    response_model=SuccessEnvelope[list[MasterExampleResponse]],
    dependencies=[Depends(require_scope("master_examples:read"))],
    responses=error_responses(*_AUTH_ERRORS, validation=True),
)
async def list_partner_master_examples(
    params: OffsetParams = Depends(),
    master_examples: MasterExampleRepositoryPort = Depends(get_master_example_repository),
) -> dict[str, Any]:
    result = await ListMasterExamplesUseCase(master_examples).execute(params)
    items = [
        MasterExampleResponse.model_validate(e).model_dump(by_alias=True) for e in result.items
    ]
    return success_envelope(
        items, request_id=get_request_context().request_id, pagination=result.to_meta()
    )


@router.get(
    "/master/examples/{example_id}",
    response_model=SuccessEnvelope[MasterExampleResponse],
    dependencies=[Depends(require_scope("master_examples:read"))],
    responses=error_responses(*_AUTH_ERRORS, MasterExampleNotFoundError, validation=True),
)
async def get_partner_master_example(
    example_id: UUID,
    master_examples: MasterExampleRepositoryPort = Depends(get_master_example_repository),
) -> dict[str, Any]:
    example = await GetMasterExampleUseCase(master_examples).execute(example_id)
    payload = MasterExampleResponse.model_validate(example).model_dump(by_alias=True)
    return success_envelope(payload, request_id=get_request_context().request_id)


@router.get(
    "/examples",
    response_model=SuccessEnvelope[list[ExampleResponse]],
    dependencies=[Depends(require_scope("examples:read"))],
    responses=error_responses(*_AUTH_ERRORS, validation=True),
)
async def list_partner_examples(
    params: OffsetParams = Depends(),
    examples: ExampleRepositoryPort = Depends(get_example_repository),
) -> dict[str, Any]:
    result = await ListExamplesUseCase(examples).execute(params)
    items = [ExampleResponse.model_validate(e).model_dump(by_alias=True) for e in result.items]
    return success_envelope(
        items, request_id=get_request_context().request_id, pagination=result.to_meta()
    )


@router.get(
    "/examples/{example_id}",
    response_model=SuccessEnvelope[ExampleResponse],
    dependencies=[Depends(require_scope("examples:read"))],
    responses=error_responses(*_AUTH_ERRORS, ExampleNotFoundError, validation=True),
)
async def get_partner_example(
    example_id: UUID, examples: ExampleRepositoryPort = Depends(get_example_repository)
) -> dict[str, Any]:
    example = await GetExampleUseCase(examples).execute(example_id)
    payload = ExampleResponse.model_validate(example).model_dump(by_alias=True)
    return success_envelope(payload, request_id=get_request_context().request_id)


# `files:read` is tenant-wide (`can_read_all`), the same grant the `files:read` permission gives a
# user: a partner owns no files, so an owner-only view would always be empty.
@router.get(
    "/files/{file_id}",
    response_model=SuccessEnvelope[FileAssetResponse],
    dependencies=[Depends(require_scope("files:read"))],
    responses=error_responses(*_AUTH_ERRORS, FileAssetNotFoundError, validation=True),
)
async def get_partner_file(
    file_id: UUID,
    api_key: ApiKey = Depends(get_current_api_client),
    files: FileRepositoryPort = Depends(get_file_repository),
) -> dict[str, Any]:
    asset, variants = await GetFileUseCase(files).execute(file_id, api_key.id, can_read_all=True)
    return success_envelope(
        file_payload(asset, variants), request_id=get_request_context().request_id
    )


@router.get(
    "/files/{file_id}/content",
    response_class=StreamingResponse,
    dependencies=[Depends(require_scope("files:read"))],
    responses=error_responses(
        *_AUTH_ERRORS, FileAssetNotFoundError, FileNotAvailableError, validation=True
    ),
)
async def get_partner_file_content(
    file_id: UUID,
    variant: str = "original",
    api_key: ApiKey = Depends(get_current_api_client),
    files: FileRepositoryPort = Depends(get_file_repository),
    storage: ObjectStoragePort = Depends(get_object_storage),
) -> StreamingResponse:
    asset, chosen = await GetFileUseCase(files).resolve_variant(
        file_id, api_key.id, can_read_all=True, variant_name=variant
    )
    return private_variant_response(storage, asset, chosen)
