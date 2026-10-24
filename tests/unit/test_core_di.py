from uuid import uuid4

import pytest
from fastapi.security import HTTPAuthorizationCredentials
from redis.asyncio import Redis
from starlette.requests import Request

from app.core.config import get_settings
from app.core.context import get_request_context, update_request_context
from app.core.di import (
    get_audit_log_repository,
    get_current_user,
    get_redis_client,
    get_role_repository,
    get_tenant_repository,
    get_token_session_store,
    get_user_repository,
    get_user_role_repository,
    record_audit_event,
    require_permission,
    require_role,
)
from app.core.security import TokenType, create_token
from app.modules.auth.domain.exceptions import InvalidTokenError
from app.modules.auth.infrastructure.token_session_store import RedisTokenSessionStore
from app.modules.roles.domain.entities import Role
from app.modules.roles.domain.exceptions import InsufficientPermissionError, RoleNotAssignedError
from app.modules.roles.infrastructure.repository import RoleRepository, UserRoleRepository
from app.modules.tenants.domain.entities import Tenant
from app.modules.tenants.infrastructure.repository import TenantRepository
from app.modules.users.domain.entities import User
from app.modules.users.infrastructure.repository import UserRepository
from tests.fakes import (
    InMemoryAuditLogRepository,
    InMemoryTenantRepository,
    InMemoryUserRepository,
    InMemoryUserRoleRepository,
)
from tests.helpers import HASHED_PASSWORD

SETTINGS = get_settings()


def _request(*headers: tuple[str, str]) -> Request:
    encoded = [(k.lower().encode(), v.encode()) for k, v in headers]
    return Request({"type": "http", "method": "GET", "path": "/", "headers": encoded})


def _access_token(user: User) -> str:
    return create_token(
        settings=SETTINGS,
        subject=user.id,
        token_type=TokenType.ACCESS,
        jti=str(uuid4()),
        tenant_id=user.tenant_id,
    )


def _credentials(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


async def _tenants_for(user: User, *, is_active: bool = True) -> InMemoryTenantRepository:
    tenants = InMemoryTenantRepository()
    await tenants.add(
        Tenant(id=user.tenant_id, name="T", slug=f"t-{user.tenant_id.hex[:8]}", is_active=is_active)
    )
    return tenants


async def test_get_current_user_garbage_token_raises() -> None:
    users = InMemoryUserRepository()
    with pytest.raises(InvalidTokenError):
        await get_current_user(
            _request(), _credentials("not-a-jwt"), settings=SETTINGS, users=users
        )


async def test_get_current_user_unknown_user_raises() -> None:
    users = InMemoryUserRepository()
    ghost = User(
        tenant_id=uuid4(), email="ghost@example.com", hashed_password=HASHED_PASSWORD, full_name="G"
    )
    token = _access_token(ghost)
    with pytest.raises(InvalidTokenError):
        await get_current_user(_request(), _credentials(token), settings=SETTINGS, users=users)


async def test_get_current_user_inactive_user_raises() -> None:
    users = InMemoryUserRepository()
    user = User(
        tenant_id=uuid4(),
        email="inactive@example.com",
        hashed_password=HASHED_PASSWORD,
        full_name="I",
        is_active=False,
    )
    await users.add(user)
    token = _access_token(user)
    with pytest.raises(InvalidTokenError):
        await get_current_user(_request(), _credentials(token), settings=SETTINGS, users=users)


async def test_get_current_user_success_without_headers_uses_user_preferences() -> None:
    users = InMemoryUserRepository()
    user = User(
        tenant_id=uuid4(),
        email="pref@example.com",
        hashed_password=HASHED_PASSWORD,
        full_name="P",
        locale="id",
        timezone="Asia/Jakarta",
    )
    await users.add(user)
    tenants = await _tenants_for(user)
    token = _access_token(user)

    result = await get_current_user(
        _request(), _credentials(token), settings=SETTINGS, users=users, tenants=tenants
    )

    assert result.id == user.id
    ctx = get_request_context()
    assert ctx.locale == "id"
    assert str(ctx.timezone) == "Asia/Jakarta"


async def test_get_current_user_success_with_headers_keeps_negotiated_values() -> None:
    users = InMemoryUserRepository()
    user = User(
        tenant_id=uuid4(),
        email="headers@example.com",
        hashed_password=HASHED_PASSWORD,
        full_name="H",
        locale="id",
        timezone="Asia/Jakarta",
    )
    await users.add(user)
    tenants = await _tenants_for(user)
    token = _access_token(user)

    await get_current_user(
        _request(("Accept-Language", "en"), ("X-Timezone", "UTC")),
        _credentials(token),
        settings=SETTINGS,
        users=users,
        tenants=tenants,
    )

    ctx = get_request_context()
    assert ctx.locale == "en"
    assert str(ctx.timezone) == "UTC"


async def test_get_current_user_invalid_stored_timezone_is_suppressed() -> None:
    users = InMemoryUserRepository()
    user = User(
        tenant_id=uuid4(),
        email="badtz@example.com",
        hashed_password=HASHED_PASSWORD,
        full_name="B",
        timezone="Not/AZone",
    )
    await users.add(user)
    tenants = await _tenants_for(user)
    token = _access_token(user)

    await get_current_user(
        _request(), _credentials(token), settings=SETTINGS, users=users, tenants=tenants
    )

    ctx = get_request_context()
    assert str(ctx.timezone) == "UTC"


async def test_get_current_user_inactive_tenant_raises() -> None:
    users = InMemoryUserRepository()
    user = User(tenant_id=uuid4(), email="t@example.com", hashed_password="x", full_name="T")
    await users.add(user)
    tenants = await _tenants_for(user, is_active=False)
    with pytest.raises(InvalidTokenError):
        await get_current_user(
            _request(),
            _credentials(_access_token(user)),
            settings=SETTINGS,
            users=users,
            tenants=tenants,
        )


async def test_get_current_user_missing_or_deleted_tenant_raises() -> None:
    users = InMemoryUserRepository()
    user = User(tenant_id=uuid4(), email="t2@example.com", hashed_password="x", full_name="T")
    await users.add(user)
    with pytest.raises(InvalidTokenError):
        await get_current_user(
            _request(),
            _credentials(_access_token(user)),
            settings=SETTINGS,
            users=users,
            tenants=InMemoryTenantRepository(),
        )


async def test_get_current_user_token_without_tenant_raises() -> None:
    users = InMemoryUserRepository()
    user = User(tenant_id=uuid4(), email="t3@example.com", hashed_password="x", full_name="T")
    await users.add(user)
    token = create_token(
        settings=SETTINGS, subject=user.id, token_type=TokenType.ACCESS, jti=str(uuid4())
    )
    with pytest.raises(InvalidTokenError):
        await get_current_user(
            _request(),
            _credentials(token),
            settings=SETTINGS,
            users=users,
            tenants=await _tenants_for(user),
        )


async def _assigned(user: User, role_name: str) -> InMemoryUserRoleRepository:
    user_roles = InMemoryUserRoleRepository()
    role = Role(tenant_id=user.tenant_id, name=role_name)
    user_roles.seed_role(role)
    await user_roles.assign(user.id, role.id)
    return user_roles


async def test_require_role_allows_matching_active_role() -> None:
    user = User(tenant_id=uuid4(), email="a@example.com", hashed_password="x", full_name="A")
    update_request_context(active_role="admin")
    dependency = require_role("admin", "owner")
    result = await dependency(user=user, user_roles=await _assigned(user, "admin"))
    assert result is user


async def test_require_role_rejects_non_matching_active_role() -> None:
    user = User(tenant_id=uuid4(), email="a@example.com", hashed_password="x", full_name="A")
    update_request_context(active_role="viewer")
    dependency = require_role("admin", "owner")
    with pytest.raises(RoleNotAssignedError):
        await dependency(user=user, user_roles=await _assigned(user, "viewer"))


async def test_require_role_rejects_when_no_active_role_claim() -> None:
    user = User(tenant_id=uuid4(), email="a@example.com", hashed_password="x", full_name="A")
    dependency = require_role("admin")
    with pytest.raises(RoleNotAssignedError):
        await dependency(user=user, user_roles=await _assigned(user, "admin"))


async def test_require_role_rejects_role_no_longer_assigned_despite_jwt_claim() -> None:
    """The JWT still says `admin`, but the assignment was revoked / the role deleted."""
    user = User(tenant_id=uuid4(), email="a@example.com", hashed_password="x", full_name="A")
    update_request_context(active_role="admin")
    dependency = require_role("admin")
    with pytest.raises(RoleNotAssignedError):
        await dependency(user=user, user_roles=InMemoryUserRoleRepository())


async def test_require_permission_allows_when_role_grants_it() -> None:
    user = User(tenant_id=uuid4(), email="a@example.com", hashed_password="x", full_name="A")
    user_roles = InMemoryUserRoleRepository()
    role = Role(tenant_id=user.tenant_id, name="editor", permissions=["users:read"])
    user_roles.seed_role(role)
    await user_roles.assign(user.id, role.id)

    dependency = require_permission("users:read")
    result = await dependency(user=user, user_roles=user_roles)
    assert result is user


async def test_require_permission_rejects_when_no_role_grants_it() -> None:
    user = User(tenant_id=uuid4(), email="a@example.com", hashed_password="x", full_name="A")
    user_roles = InMemoryUserRoleRepository()

    dependency = require_permission("users:delete")
    with pytest.raises(InsufficientPermissionError):
        await dependency(user=user, user_roles=user_roles)


async def test_record_audit_event_captures_context() -> None:
    audit_logs = InMemoryAuditLogRepository()
    tenant_id, user_id = uuid4(), uuid4()
    update_request_context(tenant_id=tenant_id, user_id=user_id)

    await record_audit_event(
        audit_logs,
        action="user.updated",
        resource_type="user",
        resource_id="abc",
        before={"name": "old"},
        after={"name": "new"},
    )

    assert len(audit_logs.records) == 1
    entry = audit_logs.records[0]
    assert entry.tenant_id == tenant_id
    assert entry.actor_id == user_id
    assert entry.action == "user.updated"
    assert entry.before == {"name": "old"}
    assert entry.after == {"name": "new"}


def test_get_user_repository_returns_concrete_type() -> None:
    assert isinstance(get_user_repository(session=object()), UserRepository)  # type: ignore[arg-type]


def test_get_role_repository_returns_concrete_type() -> None:
    assert isinstance(get_role_repository(session=object()), RoleRepository)  # type: ignore[arg-type]


def test_get_tenant_repository_returns_concrete_type() -> None:
    assert isinstance(get_tenant_repository(session=object()), TenantRepository)  # type: ignore[arg-type]


def test_get_user_role_repository_returns_concrete_type() -> None:
    assert isinstance(
        get_user_role_repository(session=object()),
        UserRoleRepository,  # type: ignore[arg-type]
    )


def test_get_audit_log_repository_returns_concrete_type() -> None:
    from app.modules.audit_log.infrastructure.repository import AuditLogRepository

    assert isinstance(
        get_audit_log_repository(session=object()),
        AuditLogRepository,  # type: ignore[arg-type]
    )


def test_get_redis_client_returns_redis_instance() -> None:
    assert isinstance(get_redis_client(), Redis)


def test_get_token_session_store_wraps_redis() -> None:
    store = get_token_session_store(redis=get_redis_client())
    assert isinstance(store, RedisTokenSessionStore)


def test_get_posts_api_port_returns_concrete_type() -> None:
    from app.core.di import get_posts_api_port
    from app.modules.posts.infrastructure.jsonplaceholder_adapter import (
        JsonPlaceholderPostsAdapter,
    )

    assert isinstance(get_posts_api_port(settings=get_settings()), JsonPlaceholderPostsAdapter)


def test_get_malware_scanner_returns_the_configured_backend() -> None:
    from app.core.di import get_malware_scanner
    from app.modules.files.infrastructure.scanner import NoopScanner

    assert isinstance(get_malware_scanner(), NoopScanner)  # default backend is "none"
