from uuid import UUID, uuid4

import pytest

from app.core.config import get_settings
from app.core.context import get_request_context
from app.core.security import TokenType, create_token, hash_password, verify_password
from app.modules.auth.application.dto import LoginInput, RegisterInput
from app.modules.auth.application.use_cases import (
    LoginUseCase,
    LogoutUseCase,
    RefreshTokenUseCase,
    RegisterUseCase,
    SwitchRoleUseCase,
)
from app.modules.auth.domain.exceptions import (
    AccountDisabledError,
    InvalidCredentialsError,
    InvalidTokenError,
    TokenRevokedError,
)
from app.modules.roles.application.use_cases import SwitchActiveRoleUseCase
from app.modules.roles.domain.entities import Role
from app.modules.roles.domain.exceptions import RoleNotAssignedError
from app.modules.tenants.domain.entities import Tenant
from app.modules.tenants.domain.exceptions import TenantInactiveError, TenantNotFoundError
from app.modules.users.domain.entities import User
from app.modules.users.domain.exceptions import EmailAlreadyRegisteredError
from tests.fakes import (
    InMemoryRoleRepository,
    InMemoryTenantRepository,
    InMemoryTokenSessionStore,
    InMemoryUserRepository,
    InMemoryUserRoleRepository,
)

SETTINGS = get_settings()


async def _seeded_tenants(*tenant_ids: UUID, is_active: bool = True) -> InMemoryTenantRepository:
    tenants = InMemoryTenantRepository()
    for tenant_id in tenant_ids:
        await tenants.add(
            Tenant(id=tenant_id, name="T", slug=f"t-{tenant_id.hex[:8]}", is_active=is_active)
        )
    return tenants


async def test_register_success_hashes_password() -> None:
    users = InMemoryUserRepository()
    roles = InMemoryRoleRepository()
    user_roles = InMemoryUserRoleRepository()
    tenant_id = uuid4()
    tenants = await _seeded_tenants(tenant_id)
    user = await RegisterUseCase(users, roles, user_roles, tenants).execute(
        RegisterInput(
            tenant_id=tenant_id, email="a@example.com", password="S3curePass!", full_name="A"
        )
    )
    assert user.hashed_password != "S3curePass!"
    assert verify_password("S3curePass!", user.hashed_password)


async def test_register_duplicate_email_same_tenant_raises() -> None:
    users = InMemoryUserRepository()
    roles = InMemoryRoleRepository()
    user_roles = InMemoryUserRoleRepository()
    tenant_id = uuid4()
    tenants = await _seeded_tenants(tenant_id)
    data = RegisterInput(tenant_id=tenant_id, email="dup@example.com", password="x", full_name="A")
    await RegisterUseCase(users, roles, user_roles, tenants).execute(data)
    with pytest.raises(EmailAlreadyRegisteredError):
        await RegisterUseCase(users, roles, user_roles, tenants).execute(data)


async def test_register_same_email_different_tenant_succeeds() -> None:
    users = InMemoryUserRepository()
    roles = InMemoryRoleRepository()
    user_roles = InMemoryUserRoleRepository()
    email = "shared@example.com"
    tenant_a, tenant_b = uuid4(), uuid4()
    tenants = await _seeded_tenants(tenant_a, tenant_b)
    await RegisterUseCase(users, roles, user_roles, tenants).execute(
        RegisterInput(tenant_id=tenant_a, email=email, password="x", full_name="A")
    )
    user_b = await RegisterUseCase(users, roles, user_roles, tenants).execute(
        RegisterInput(tenant_id=tenant_b, email=email, password="y", full_name="B")
    )
    assert user_b.email == email


async def test_register_assigns_default_user_role_when_it_exists() -> None:
    users = InMemoryUserRepository()
    roles = InMemoryRoleRepository()
    user_roles = InMemoryUserRoleRepository()
    tenant_id = uuid4()
    tenants = await _seeded_tenants(tenant_id)
    default_role = await roles.add(Role(tenant_id=tenant_id, name="user", permissions=[]))
    user_roles.seed_role(default_role)

    user = await RegisterUseCase(users, roles, user_roles, tenants).execute(
        RegisterInput(tenant_id=tenant_id, email="new@example.com", password="x", full_name="A")
    )

    assigned = await user_roles.list_roles_for_user(user.id)
    assert [r.name for r in assigned] == ["user"]


async def test_register_without_default_role_still_succeeds() -> None:
    users = InMemoryUserRepository()
    roles = InMemoryRoleRepository()
    user_roles = InMemoryUserRoleRepository()
    tenant_id = uuid4()
    tenants = await _seeded_tenants(tenant_id)

    user = await RegisterUseCase(users, roles, user_roles, tenants).execute(
        RegisterInput(tenant_id=tenant_id, email="new@example.com", password="x", full_name="A")
    )

    assert await user_roles.list_roles_for_user(user.id) == []


async def test_register_unknown_tenant_raises_and_creates_no_user() -> None:
    users = InMemoryUserRepository()
    roles = InMemoryRoleRepository()
    user_roles = InMemoryUserRoleRepository()
    tenants = await _seeded_tenants(uuid4())  # some other tenant exists, not this one

    with pytest.raises(TenantNotFoundError):
        await RegisterUseCase(users, roles, user_roles, tenants).execute(
            RegisterInput(tenant_id=uuid4(), email="x@example.com", password="x", full_name="A")
        )

    assert users.users == {}


async def test_register_inactive_tenant_is_reported_as_not_found_and_creates_no_user() -> None:
    users = InMemoryUserRepository()
    roles = InMemoryRoleRepository()
    user_roles = InMemoryUserRoleRepository()
    tenant_id = uuid4()
    tenants = await _seeded_tenants(tenant_id, is_active=False)

    with pytest.raises(TenantNotFoundError):
        await RegisterUseCase(users, roles, user_roles, tenants).execute(
            RegisterInput(tenant_id=tenant_id, email="x@example.com", password="x", full_name="A")
        )

    assert users.users == {}


def _build_user(
    tenant_id: object = None,
    *,
    password: str = "S3curePass!",  # noqa: S107 - test fixture, not a real credential
    is_active: bool = True,
) -> User:
    return User(
        tenant_id=tenant_id or uuid4(),
        email="login@example.com",
        hashed_password=hash_password(password),
        full_name="Login User",
        is_active=is_active,
    )


async def test_login_unknown_email_raises() -> None:
    users = InMemoryUserRepository()
    tenants = InMemoryTenantRepository()
    user_roles = InMemoryUserRoleRepository()
    sessions = InMemoryTokenSessionStore()
    with pytest.raises(InvalidCredentialsError):
        await LoginUseCase(users, user_roles, SETTINGS, sessions, tenants).execute(
            LoginInput(tenant_id=uuid4(), email="nope@example.com", password="x")
        )


async def test_login_wrong_password_raises() -> None:
    users = InMemoryUserRepository()
    user = _build_user()
    await users.add(user)
    tenants = await _seeded_tenants(user.tenant_id)
    user_roles = InMemoryUserRoleRepository()
    sessions = InMemoryTokenSessionStore()
    with pytest.raises(InvalidCredentialsError):
        await LoginUseCase(users, user_roles, SETTINGS, sessions, tenants).execute(
            LoginInput(tenant_id=user.tenant_id, email=user.email, password="wrong")
        )


async def test_login_inactive_account_raises() -> None:
    users = InMemoryUserRepository()
    user = _build_user(is_active=False)
    await users.add(user)
    tenants = await _seeded_tenants(user.tenant_id)
    user_roles = InMemoryUserRoleRepository()
    sessions = InMemoryTokenSessionStore()
    with pytest.raises(AccountDisabledError):
        await LoginUseCase(users, user_roles, SETTINGS, sessions, tenants).execute(
            LoginInput(tenant_id=user.tenant_id, email=user.email, password="S3curePass!")
        )


async def test_login_inactive_tenant_raises() -> None:
    users = InMemoryUserRepository()
    user = _build_user()
    await users.add(user)
    tenants = await _seeded_tenants(user.tenant_id, is_active=False)
    with pytest.raises(TenantInactiveError):
        await LoginUseCase(
            users, InMemoryUserRoleRepository(), SETTINGS, InMemoryTokenSessionStore(), tenants
        ).execute(LoginInput(tenant_id=user.tenant_id, email=user.email, password="S3curePass!"))


async def test_login_inactive_tenant_with_wrong_password_does_not_leak() -> None:
    """Bad credentials must still look like bad credentials, so an
    unauthenticated caller can't tell that a tenant is suspended."""
    users = InMemoryUserRepository()
    user = _build_user()
    await users.add(user)
    tenants = await _seeded_tenants(user.tenant_id, is_active=False)
    with pytest.raises(InvalidCredentialsError):
        await LoginUseCase(
            users, InMemoryUserRoleRepository(), SETTINGS, InMemoryTokenSessionStore(), tenants
        ).execute(LoginInput(tenant_id=user.tenant_id, email=user.email, password="wrong"))


async def test_login_success_without_roles() -> None:
    users = InMemoryUserRepository()
    user = _build_user()
    await users.add(user)
    tenants = await _seeded_tenants(user.tenant_id)
    user_roles = InMemoryUserRoleRepository()
    sessions = InMemoryTokenSessionStore()

    logged_in_user, tokens = await LoginUseCase(
        users, user_roles, SETTINGS, sessions, tenants
    ).execute(LoginInput(tenant_id=user.tenant_id, email=user.email, password="S3curePass!"))

    assert logged_in_user.id == user.id
    assert tokens.access_token
    assert tokens.refresh_token
    # tenant_id is passed explicitly to the tenant-scoped role lookup
    # (see LoginUseCase.execute) rather than mutated into ambient request
    # context, so a successful login with no MissingTenantScopeError here
    # is itself the proof that threading works — context stays untouched.
    assert get_request_context().tenant_id is None


async def test_login_success_with_roles_sets_active_role() -> None:
    users = InMemoryUserRepository()
    user = _build_user()
    await users.add(user)
    tenants = await _seeded_tenants(user.tenant_id)
    user_roles = InMemoryUserRoleRepository()
    role = Role(tenant_id=user.tenant_id, name="editor", permissions=["users:read"])
    user_roles.seed_role(role)
    await user_roles.assign(user.id, role.id)
    sessions = InMemoryTokenSessionStore()

    _, tokens = await LoginUseCase(users, user_roles, SETTINGS, sessions, tenants).execute(
        LoginInput(tenant_id=user.tenant_id, email=user.email, password="S3curePass!")
    )
    payload = _decode_access(tokens.access_token)
    assert payload["active_role"] == "editor"


def _decode_access(token: str) -> dict[str, object]:
    import jwt

    return jwt.decode(token, SETTINGS.secret_key, algorithms=[SETTINGS.jwt_algorithm])


async def test_refresh_garbage_token_raises() -> None:
    users = InMemoryUserRepository()
    tenants = InMemoryTenantRepository()
    sessions = InMemoryTokenSessionStore()
    with pytest.raises(InvalidTokenError):
        await RefreshTokenUseCase(
            users, SETTINGS, sessions, tenants, InMemoryUserRoleRepository()
        ).execute("not-a-jwt")


async def test_refresh_with_access_token_raises() -> None:
    users = InMemoryUserRepository()
    tenants = InMemoryTenantRepository()
    sessions = InMemoryTokenSessionStore()
    token = create_token(settings=SETTINGS, subject=uuid4(), token_type=TokenType.ACCESS, jti="abc")
    with pytest.raises(InvalidTokenError):
        await RefreshTokenUseCase(
            users, SETTINGS, sessions, tenants, InMemoryUserRoleRepository()
        ).execute(token)


async def test_refresh_revoked_jti_raises() -> None:
    users = InMemoryUserRepository()
    user = _build_user()
    await users.add(user)
    tenants = await _seeded_tenants(user.tenant_id)
    sessions = InMemoryTokenSessionStore()
    token = create_token(
        settings=SETTINGS, subject=user.id, token_type=TokenType.REFRESH, jti="not-stored"
    )
    with pytest.raises(TokenRevokedError):
        await RefreshTokenUseCase(
            users, SETTINGS, sessions, tenants, InMemoryUserRoleRepository()
        ).execute(token)


async def test_refresh_unknown_user_raises() -> None:
    users = InMemoryUserRepository()
    tenants = InMemoryTenantRepository()
    sessions = InMemoryTokenSessionStore()
    user_id = uuid4()
    await sessions.store_refresh_token(user_id=user_id, jti="jti1", expires_at=_far_future())
    token = create_token(
        settings=SETTINGS, subject=user_id, token_type=TokenType.REFRESH, jti="jti1"
    )
    with pytest.raises(InvalidTokenError):
        await RefreshTokenUseCase(
            users, SETTINGS, sessions, tenants, InMemoryUserRoleRepository()
        ).execute(token)


async def test_refresh_inactive_user_raises() -> None:
    users = InMemoryUserRepository()
    user = _build_user(is_active=False)
    await users.add(user)
    tenants = await _seeded_tenants(user.tenant_id)
    sessions = InMemoryTokenSessionStore()
    await sessions.store_refresh_token(user_id=user.id, jti="jti1", expires_at=_far_future())
    token = create_token(
        settings=SETTINGS, subject=user.id, token_type=TokenType.REFRESH, jti="jti1"
    )
    with pytest.raises(InvalidTokenError):
        await RefreshTokenUseCase(
            users, SETTINGS, sessions, tenants, InMemoryUserRoleRepository()
        ).execute(token)


async def test_refresh_inactive_tenant_raises() -> None:
    users = InMemoryUserRepository()
    user = _build_user()
    await users.add(user)
    tenants = await _seeded_tenants(user.tenant_id, is_active=False)
    sessions = InMemoryTokenSessionStore()
    await sessions.store_refresh_token(user_id=user.id, jti="jti1", expires_at=_far_future())
    token = create_token(
        settings=SETTINGS, subject=user.id, token_type=TokenType.REFRESH, jti="jti1"
    )
    with pytest.raises(TenantInactiveError):
        await RefreshTokenUseCase(
            users, SETTINGS, sessions, tenants, InMemoryUserRoleRepository()
        ).execute(token)


async def test_refresh_success_rotates_jti_and_keeps_active_role() -> None:
    users = InMemoryUserRepository()
    user = _build_user()
    await users.add(user)
    tenants = await _seeded_tenants(user.tenant_id)
    sessions = InMemoryTokenSessionStore()
    user_roles = InMemoryUserRoleRepository()
    editor = Role(tenant_id=user.tenant_id, name="editor", permissions=[])
    user_roles.seed_role(editor)
    await user_roles.assign(user.id, editor.id)  # the claim below is still an assigned role
    await sessions.store_refresh_token(user_id=user.id, jti="jti1", expires_at=_far_future())
    token = create_token(
        settings=SETTINGS,
        subject=user.id,
        token_type=TokenType.REFRESH,
        jti="jti1",
        active_role="editor",
    )

    tokens = await RefreshTokenUseCase(users, SETTINGS, sessions, tenants, user_roles).execute(
        token
    )

    assert (user.id, "jti1") not in sessions.valid
    new_payload = _decode_access(tokens.access_token)
    assert new_payload["active_role"] == "editor"


async def test_refresh_reusing_an_already_consumed_token_raises() -> None:
    """Regression test for the rotation TOCTOU: consuming a refresh token
    must be atomic (check-and-revoke in one step), so a second attempt
    with the same jti — whether a genuine reuse or a concurrent request
    racing the first — is rejected rather than minting a second valid
    token pair from one refresh token."""
    users = InMemoryUserRepository()
    user = _build_user()
    await users.add(user)
    tenants = await _seeded_tenants(user.tenant_id)
    sessions = InMemoryTokenSessionStore()
    await sessions.store_refresh_token(user_id=user.id, jti="jti1", expires_at=_far_future())
    token = create_token(
        settings=SETTINGS, subject=user.id, token_type=TokenType.REFRESH, jti="jti1"
    )

    await RefreshTokenUseCase(
        users, SETTINGS, sessions, tenants, InMemoryUserRoleRepository()
    ).execute(token)

    with pytest.raises(TokenRevokedError):
        await RefreshTokenUseCase(
            users, SETTINGS, sessions, tenants, InMemoryUserRoleRepository()
        ).execute(token)


async def test_logout_invalid_token_is_noop() -> None:
    sessions = InMemoryTokenSessionStore()
    await LogoutUseCase(SETTINGS, sessions).execute("garbage")
    assert sessions.valid == set()


async def test_logout_valid_token_revokes() -> None:
    sessions = InMemoryTokenSessionStore()
    user_id = uuid4()
    await sessions.store_refresh_token(user_id=user_id, jti="jti1", expires_at=_far_future())
    token = create_token(
        settings=SETTINGS, subject=user_id, token_type=TokenType.REFRESH, jti="jti1"
    )

    await LogoutUseCase(SETTINGS, sessions).execute(token)

    assert (user_id, "jti1") not in sessions.valid


async def test_switch_role_unassigned_raises() -> None:
    users = InMemoryUserRepository()
    user = _build_user()
    await users.add(user)
    sessions = InMemoryTokenSessionStore()
    user_roles = InMemoryUserRoleRepository()
    switch_role = SwitchActiveRoleUseCase(user_roles)

    with pytest.raises(RoleNotAssignedError):
        await SwitchRoleUseCase(users, SETTINGS, sessions, switch_role).execute(user.id, "admin")


async def test_switch_role_success_reissues_tokens() -> None:
    users = InMemoryUserRepository()
    user = _build_user()
    await users.add(user)
    sessions = InMemoryTokenSessionStore()
    user_roles = InMemoryUserRoleRepository()
    role = Role(tenant_id=user.tenant_id, name="admin", permissions=["*"])
    user_roles.seed_role(role)
    await user_roles.assign(user.id, role.id)
    switch_role = SwitchActiveRoleUseCase(user_roles)

    tokens = await SwitchRoleUseCase(users, SETTINGS, sessions, switch_role).execute(
        user.id, "admin"
    )
    payload = _decode_access(tokens.access_token)
    assert payload["active_role"] == "admin"


def _far_future() -> object:
    from datetime import UTC, datetime, timedelta

    return datetime.now(UTC) + timedelta(days=1)
