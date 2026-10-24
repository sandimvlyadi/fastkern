"""Composition root: wires concrete repository/service implementations to
the ports each module's application layer depends on.

This is the ONE place allowed to import infrastructure from more than one
module — every module's presentation layer depends on providers *here*
(e.g. `get_current_user`, `require_role`) instead of reaching into a
sibling module's infrastructure package directly. That is what the
import-linter "modules must not import each other's infrastructure"
contract in pyproject.toml enforces.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any
from uuid import UUID

from fastapi import Depends, Request
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import get_redis
from app.core.config import Settings, get_settings
from app.core.context import get_request_context, update_request_context
from app.core.db import get_db_session
from app.core.security import TokenError, TokenType, decode_token
from app.core.timezone import parse_timezone
from app.modules.api_clients.application.use_cases import VerifyApiKeyUseCase
from app.modules.api_clients.domain.entities import ApiKey
from app.modules.api_clients.domain.exceptions import InvalidApiKeyError, ScopeDeniedError
from app.modules.api_clients.domain.ports import ApiKeyRepositoryPort
from app.modules.api_clients.infrastructure.repository import ApiKeyRepository
from app.modules.audit_log.application.dto import RecordAuditEventInput
from app.modules.audit_log.application.use_cases import RecordAuditEventUseCase
from app.modules.audit_log.domain.ports import AuditLogRepositoryPort
from app.modules.audit_log.infrastructure.repository import AuditLogRepository
from app.modules.auth.domain.exceptions import InvalidTokenError
from app.modules.auth.domain.ports import TokenSessionStorePort
from app.modules.auth.infrastructure.token_session_store import RedisTokenSessionStore
from app.modules.examples.domain.ports import ExampleRepositoryPort
from app.modules.examples.infrastructure.repository import ExampleRepository
from app.modules.files.application.use_cases import UploadPolicy
from app.modules.files.domain.ports import (
    ChunkRepositoryPort,
    FileRepositoryPort,
    MalwareScannerPort,
    ObjectStoragePort,
    UploadRepositoryPort,
)

# Every ORM model must be imported once before the metadata is used (workers, migrations, tests),
# because foreign keys resolve against it. The repositories imported here pull in the rest.
from app.modules.files.infrastructure import models as _files_models  # noqa: F401
from app.modules.files.infrastructure.factory import (
    build_malware_scanner,
    build_object_storage,
    build_upload_policy,
)
from app.modules.files.infrastructure.repository import (
    ChunkRepository,
    FileRepository,
    UploadRepository,
)
from app.modules.master_examples.domain.ports import MasterExampleRepositoryPort
from app.modules.master_examples.infrastructure.repository import MasterExampleRepository
from app.modules.posts.domain.ports import PostsApiPort
from app.modules.posts.infrastructure.jsonplaceholder_adapter import JsonPlaceholderPostsAdapter
from app.modules.roles.application.use_cases import ProvisionBaselineRolesUseCase
from app.modules.roles.domain.exceptions import InsufficientPermissionError, RoleNotAssignedError
from app.modules.roles.domain.ports import RoleRepositoryPort, UserRoleRepositoryPort
from app.modules.roles.infrastructure.repository import RoleRepository, UserRoleRepository
from app.modules.tenants.domain.ports import TenantRepositoryPort, TenantRoleProvisionerPort
from app.modules.tenants.infrastructure.repository import TenantRepository
from app.modules.users.domain.entities import User
from app.modules.users.domain.ports import UserRepositoryPort
from app.modules.users.infrastructure.repository import UserRepository

_bearer_scheme = HTTPBearer(auto_error=True)


# --- Repository providers -------------------------------------------------


def get_user_repository(
    session: AsyncSession = Depends(get_db_session, scope="function"),
) -> UserRepositoryPort:
    return UserRepository(session)


def get_tenant_repository(
    session: AsyncSession = Depends(get_db_session, scope="function"),
) -> TenantRepositoryPort:
    return TenantRepository(session)


def get_posts_api_port(settings: Settings = Depends(get_settings)) -> PostsApiPort:
    return JsonPlaceholderPostsAdapter(
        base_url=settings.posts_api_base_url,
        auth_header=settings.posts_api_auth_header,
        auth_token=settings.posts_api_auth_token.get_secret_value(),
        timeout_seconds=settings.posts_api_timeout_seconds,
    )


def get_role_repository(
    session: AsyncSession = Depends(get_db_session, scope="function"),
) -> RoleRepositoryPort:
    return RoleRepository(session)


def get_master_example_repository(
    session: AsyncSession = Depends(get_db_session, scope="function"),
) -> MasterExampleRepositoryPort:
    return MasterExampleRepository(session)


def get_example_repository(
    session: AsyncSession = Depends(get_db_session, scope="function"),
) -> ExampleRepositoryPort:
    return ExampleRepository(session)


def get_user_role_repository(
    session: AsyncSession = Depends(get_db_session, scope="function"),
) -> UserRoleRepositoryPort:
    return UserRoleRepository(session)


class _RolesBackedTenantRoleProvisioner(TenantRoleProvisionerPort):
    """Joins the tenants module's port to the roles module's use case. The composition root
    is the one place that may know both, so neither module imports the other's internals."""

    def __init__(self, roles: RoleRepositoryPort) -> None:
        self._use_case = ProvisionBaselineRolesUseCase(roles)

    async def provision_baseline_roles(self, tenant_id: UUID) -> None:
        await self._use_case.execute(tenant_id)


def get_tenant_role_provisioner(
    roles: RoleRepositoryPort = Depends(get_role_repository),
) -> TenantRoleProvisionerPort:
    return _RolesBackedTenantRoleProvisioner(roles)


def get_redis_client() -> Redis:  # type: ignore[type-arg]
    return get_redis()


def get_token_session_store(
    redis: Redis = Depends(get_redis_client),  # type: ignore[type-arg]
) -> RedisTokenSessionStore:
    return RedisTokenSessionStore(redis)


def get_audit_log_repository(
    session: AsyncSession = Depends(get_db_session, scope="function"),
) -> AuditLogRepositoryPort:
    return AuditLogRepository(session)


def get_api_key_repository(
    session: AsyncSession = Depends(get_db_session, scope="function"),
) -> ApiKeyRepositoryPort:
    return ApiKeyRepository(session)


def get_malware_scanner() -> MalwareScannerPort:
    # The scanner uploads use, so the ops heartbeat self-tests what the pipeline actually calls.
    return build_malware_scanner(get_settings())


def get_upload_repository(
    session: AsyncSession = Depends(get_db_session, scope="function"),
) -> UploadRepositoryPort:
    return UploadRepository(session)


def get_chunk_repository(
    session: AsyncSession = Depends(get_db_session, scope="function"),
) -> ChunkRepositoryPort:
    return ChunkRepository(session)


def get_file_repository(
    session: AsyncSession = Depends(get_db_session, scope="function"),
) -> FileRepositoryPort:
    return FileRepository(session)


def get_object_storage(settings: Settings = Depends(get_settings)) -> ObjectStoragePort:
    # Deliberately duplicates `files.presentation.router.get_object_storage` (same body): each
    # module's presentation layer depends on its own DI provider rather than importing another
    # module's presentation layer.
    return build_object_storage(settings)


def get_upload_policy(settings: Settings = Depends(get_settings)) -> UploadPolicy:
    return build_upload_policy(settings)


async def record_audit_event(
    audit_logs: AuditLogRepositoryPort,
    *,
    action: str,
    resource_type: str,
    resource_id: str | None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    """Call this from a mutating endpoint right after the use case
    succeeds. Every module's router follows this same one-line pattern —
    see `app.modules.users.presentation.router` for a worked example.

    Attributes the event to whichever principal authenticated the request: an `ApiKey` if
    `get_current_api_client` ran, otherwise a `User`. The two never both populate the request
    context on one request, since a single route is either partner-authenticated (API key) or
    user-authenticated (JWT/RBAC), never both."""
    ctx = get_request_context()
    actor_type = "api_client" if ctx.api_client_id is not None else "user"
    actor_id = ctx.api_client_id if actor_type == "api_client" else ctx.user_id
    await RecordAuditEventUseCase(audit_logs).execute(
        RecordAuditEventInput(
            tenant_id=ctx.tenant_id,
            actor_id=actor_id,
            actor_type=actor_type,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            before=before,
            after=after,
            request_id=ctx.request_id,
        )
    )


# --- Authentication / authorization ---------------------------------------


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
    settings: Settings = Depends(get_settings),
    users: UserRepositoryPort = Depends(get_user_repository),
    tenants: TenantRepositoryPort = Depends(get_tenant_repository),
    sessions: TokenSessionStorePort = Depends(get_token_session_store),
) -> User:
    try:
        payload = decode_token(
            settings=settings, token=credentials.credentials, expected_type=TokenType.ACCESS
        )
    except TokenError as exc:
        raise InvalidTokenError() from exc

    # Logout (or a refresh-token reuse) revokes the *session*, not just the refresh token; without
    # this check the access token would stay valid until it expires. One Redis round trip per
    # request, fails open if Redis is down (see RedisTokenSessionStore.is_session_revoked). Tokens
    # minted before sessions existed carry no `sid` and are only limited by their expiry.
    if payload.session_id and await sessions.is_session_revoked(session_id=payload.session_id):
        raise InvalidTokenError()

    user = await users.get_by_id(payload.subject, tenant_scoped=False)
    if user is None or not user.is_active:
        raise InvalidTokenError()
    # Suspending or deleting a tenant must cut its existing sessions too, not
    # only block new logins/refreshes — otherwise an access token stays valid
    # until it expires. Costs one indexed primary-key lookup per request.
    if payload.tenant_id is None:
        raise InvalidTokenError()
    tenant = await tenants.get_by_id(payload.tenant_id)
    if tenant is None or not tenant.is_active:
        raise InvalidTokenError()

    context_changes: dict[str, Any] = {
        "user_id": user.id,
        "tenant_id": payload.tenant_id,
        "active_role": payload.active_role,
        "session_id": payload.session_id,
    }
    # Client didn't explicitly negotiate locale/timezone via headers ->
    # fall back to the authenticated user's saved preference.
    if "Accept-Language" not in request.headers:
        context_changes["locale"] = user.locale
    if "X-Timezone" not in request.headers and (user_timezone := parse_timezone(user.timezone)):
        # A stored value can be stale or hand-edited; an unusable one just isn't applied.
        context_changes["timezone"] = user_timezone
    update_request_context(**context_changes)
    return user


def require_role(*allowed_roles: str) -> Callable[..., Coroutine[Any, Any, User]]:
    """FastAPI dependency: 403s unless the caller's *active* role (the one
    selected at login / via /auth/switch-role) is in `allowed_roles` AND is
    still assigned to them. The JWT claim alone is not enough: it outlives a
    revoked assignment or a deleted role until the token expires."""

    async def _dependency(
        user: User = Depends(get_current_user),
        user_roles: UserRoleRepositoryPort = Depends(get_user_role_repository),
    ) -> User:
        active_role = get_request_context().active_role
        if (
            active_role is None
            or active_role not in allowed_roles
            or not await user_roles.user_has_role(user.id, active_role)
        ):
            raise RoleNotAssignedError(role=" or ".join(allowed_roles))
        return user

    return _dependency


def require_permission(permission: str) -> Callable[..., Coroutine[Any, Any, User]]:
    """FastAPI dependency: 403s unless one of the caller's assigned roles
    (not just the active one) grants `permission`."""

    async def _dependency(
        user: User = Depends(get_current_user),
        user_roles: UserRoleRepositoryPort = Depends(get_user_role_repository),
    ) -> User:
        roles = await user_roles.list_roles_for_user(user.id)
        if not any(role.has_permission(permission) for role in roles):
            raise InsufficientPermissionError(permission=permission)
        return user

    return _dependency


# --- Partner API key authentication / authorization -----------------------

_api_key_header_scheme = APIKeyHeader(name="X-API-Key", auto_error=False)


async def get_current_api_client(
    request: Request,
    api_key_header: str | None = Depends(_api_key_header_scheme),
    api_keys: ApiKeyRepositoryPort = Depends(get_api_key_repository),
) -> ApiKey:
    """FastAPI dependency, structurally parallel to `get_current_user`, for the
    `partners` router: a partner authenticates with a long-lived API key
    (`X-API-Key` header) instead of a JWT, and is a distinct
    principal type (`ApiKey`, not `User`) with no RBAC role."""
    if not api_key_header:
        raise InvalidApiKeyError()
    api_key = await VerifyApiKeyUseCase(api_keys).execute(api_key_header)
    update_request_context(api_client_id=api_key.id, tenant_id=api_key.tenant_id)
    # Also on request.state: ApiTrafficLogMiddleware cannot see the contextvar set above
    # (see `ApiTrafficLogMiddleware._payload`).
    request.state.api_client_id = api_key.id
    request.state.api_client_tenant_id = api_key.tenant_id
    return api_key


def require_scope(scope: str) -> Callable[..., Coroutine[Any, Any, ApiKey]]:
    """FastAPI dependency: 403s unless the authenticated ApiKey's `scopes` grant `scope`.
    Independent of RBAC (`require_permission`) by design: an ApiKey has no role, so its
    authorization is checked against its own `scopes` list instead of a user's permissions."""

    async def _dependency(api_key: ApiKey = Depends(get_current_api_client)) -> ApiKey:
        if not api_key.has_scope(scope):
            raise ScopeDeniedError(scope=scope)
        return api_key

    return _dependency
