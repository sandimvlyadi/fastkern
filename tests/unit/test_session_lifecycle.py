"""Session revocation, refresh-token reuse detection and a non-stale
`active_role` on refresh, at the use-case level."""

from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.config import get_settings
from app.core.security import TokenType, create_token, decode_token, hash_password
from app.modules.auth.application.dto import LoginInput
from app.modules.auth.application.use_cases import (
    LoginUseCase,
    LogoutUseCase,
    RefreshTokenUseCase,
    SwitchRoleUseCase,
)
from app.modules.auth.domain.exceptions import InvalidTokenError, TokenRevokedError
from app.modules.roles.application.use_cases import SwitchActiveRoleUseCase
from app.modules.roles.domain.entities import Role
from app.modules.tenants.domain.entities import Tenant
from app.modules.users.domain.entities import User
from tests.fakes import (
    InMemoryTenantRepository,
    InMemoryTokenSessionStore,
    InMemoryUserRepository,
    InMemoryUserRoleRepository,
)

SETTINGS = get_settings()
REFRESH_TTL_SECONDS = SETTINGS.refresh_token_expire_minutes * 60
PASSWORD = "S3curePass!"


class World:
    """One active tenant with one user, plus the in-memory ports every use case here needs."""

    def __init__(self) -> None:
        self.users = InMemoryUserRepository()
        self.tenants = InMemoryTenantRepository()
        self.sessions = InMemoryTokenSessionStore()
        self.user_roles = InMemoryUserRoleRepository()
        self.tenant = Tenant(name="T", slug="t")
        self.user = User(
            tenant_id=self.tenant.id,
            email="u@example.com",
            hashed_password=hash_password(PASSWORD),
            full_name="U",
        )

    async def setup(self) -> World:
        await self.tenants.add(self.tenant)
        await self.users.add(self.user)
        return self

    async def give_role(self, name: str) -> Role:
        role = Role(tenant_id=self.tenant.id, name=name, permissions=[])
        self.user_roles.seed_role(role)
        await self.user_roles.assign(self.user.id, role.id)
        return role

    def refresh_use_case(self) -> RefreshTokenUseCase:
        return RefreshTokenUseCase(
            self.users, SETTINGS, self.sessions, self.tenants, self.user_roles
        )

    async def login(self) -> tuple[str, str]:
        _, pair = await LoginUseCase(
            self.users, self.user_roles, SETTINGS, self.sessions, self.tenants
        ).execute(LoginInput(tenant_id=self.tenant.id, email=self.user.email, password=PASSWORD))
        return pair.access_token, pair.refresh_token


def _sid(token: str, kind: TokenType) -> str | None:
    return decode_token(settings=SETTINGS, token=token, expected_type=kind).session_id


def _far_future() -> datetime:
    return datetime.now(UTC) + timedelta(days=1)


# --- the sid claim ---------------------------------------------------------------------------


async def test_login_issues_access_and_refresh_tokens_of_one_new_session() -> None:
    world = await World().setup()

    access, refresh = await world.login()
    other_access, _ = await world.login()

    sid = _sid(access, TokenType.ACCESS)
    assert sid is not None
    assert sid == _sid(refresh, TokenType.REFRESH)
    assert sid != _sid(other_access, TokenType.ACCESS)  # every login is its own session


async def test_refresh_rotation_stays_in_the_same_session() -> None:
    world = await World().setup()
    access, refresh = await world.login()

    new_pair = await world.refresh_use_case().execute(refresh)

    assert _sid(new_pair.access_token, TokenType.ACCESS) == _sid(access, TokenType.ACCESS)
    assert _sid(new_pair.refresh_token, TokenType.REFRESH) == _sid(access, TokenType.ACCESS)


async def test_refreshing_a_pre_session_token_upgrades_it_to_a_session() -> None:
    world = await World().setup()
    await world.sessions.store_refresh_token(
        user_id=world.user.id, jti="legacy", expires_at=_far_future()
    )
    legacy = create_token(
        settings=SETTINGS,
        subject=world.user.id,
        token_type=TokenType.REFRESH,
        jti="legacy",
        tenant_id=world.tenant.id,
    )
    assert _sid(legacy, TokenType.REFRESH) is None

    new_pair = await world.refresh_use_case().execute(legacy)

    assert _sid(new_pair.access_token, TokenType.ACCESS) is not None


async def test_switch_role_keeps_the_callers_session() -> None:
    world = await World().setup()
    await world.give_role("admin")
    access, _ = await world.login()
    sid = _sid(access, TokenType.ACCESS)

    pair = await SwitchRoleUseCase(
        world.users, SETTINGS, world.sessions, SwitchActiveRoleUseCase(world.user_roles)
    ).execute(world.user.id, "admin", session_id=sid)

    assert _sid(pair.access_token, TokenType.ACCESS) == sid
    assert _sid(pair.refresh_token, TokenType.REFRESH) == sid


# --- logout revokes the session -------------------------------------------------------------------


async def test_logout_revokes_the_session_for_the_refresh_token_lifetime() -> None:
    world = await World().setup()
    access, refresh = await world.login()
    other_access, _ = await world.login()

    await LogoutUseCase(SETTINGS, world.sessions).execute(refresh)

    sid = _sid(access, TokenType.ACCESS)
    assert sid is not None
    # Long enough that an older refresh token of the session (switch-role leaves one) stays dead.
    assert world.sessions.revoked_sessions == {sid: REFRESH_TTL_SECONDS}
    other_sid = _sid(other_access, TokenType.ACCESS)
    assert other_sid is not None and other_sid not in world.sessions.revoked_sessions


async def test_logout_of_a_pre_session_token_still_revokes_the_refresh_token_only() -> None:
    world = await World().setup()
    await world.sessions.store_refresh_token(
        user_id=world.user.id, jti="legacy", expires_at=_far_future()
    )
    legacy = create_token(
        settings=SETTINGS, subject=world.user.id, token_type=TokenType.REFRESH, jti="legacy"
    )

    await LogoutUseCase(SETTINGS, world.sessions).execute(legacy)

    assert (world.user.id, "legacy") not in world.sessions.valid
    assert world.sessions.revoked_sessions == {}


async def test_refresh_is_refused_for_a_revoked_session_even_with_a_live_jti() -> None:
    world = await World().setup()
    _, refresh = await world.login()
    sid = _sid(refresh, TokenType.REFRESH)
    assert sid is not None
    await world.sessions.revoke_session(session_id=sid, ttl_seconds=60)

    with pytest.raises(TokenRevokedError):
        await world.refresh_use_case().execute(refresh)


# --- reuse detection ------------------------------------------------------------------------------


async def test_reusing_a_consumed_refresh_token_revokes_the_whole_session() -> None:
    world = await World().setup()
    _, refresh_a = await world.login()
    sid = _sid(refresh_a, TokenType.REFRESH)
    pair_b = await world.refresh_use_case().execute(refresh_a)  # A -> B

    with pytest.raises(TokenRevokedError):
        await world.refresh_use_case().execute(refresh_a)  # A again: theft signature

    assert world.sessions.revoked_sessions == {sid: REFRESH_TTL_SECONDS}
    with pytest.raises(TokenRevokedError):  # B is dead too, though its jti is still stored
        await world.refresh_use_case().execute(pair_b.refresh_token)


async def test_reuse_of_one_session_leaves_the_users_other_sessions_alone() -> None:
    world = await World().setup()
    _, refresh_a = await world.login()
    _, refresh_other = await world.login()
    await world.refresh_use_case().execute(refresh_a)

    with pytest.raises(TokenRevokedError):
        await world.refresh_use_case().execute(refresh_a)

    await world.refresh_use_case().execute(refresh_other)  # unaffected


async def test_a_foreign_signed_token_revokes_nothing() -> None:
    world = await World().setup()
    await world.login()
    forged = jwt.encode(
        {
            "sub": str(world.user.id),
            "type": "refresh",
            "jti": "x",
            "sid": "victim-session",
            "exp": datetime.now(UTC) + timedelta(hours=1),
        },
        "some-other-key-entirely-not-ours-0123456789",
        algorithm=SETTINGS.jwt_algorithm,
    )

    with pytest.raises(InvalidTokenError):
        await world.refresh_use_case().execute(forged)

    assert world.sessions.revoked_sessions == {}


async def test_an_expired_token_revokes_nothing() -> None:
    world = await World().setup()
    expired = jwt.encode(
        {
            "sub": str(world.user.id),
            "type": "refresh",
            "jti": "x",
            "sid": "some-session",
            "exp": datetime.now(UTC) - timedelta(minutes=1),
        },
        SETTINGS.secret_key,
        algorithm=SETTINGS.jwt_algorithm,
    )

    with pytest.raises(InvalidTokenError):
        await world.refresh_use_case().execute(expired)

    assert world.sessions.revoked_sessions == {}


async def test_a_signed_token_without_a_session_claim_is_refused_without_revoking_anything() -> (
    None
):
    world = await World().setup()
    unknown_jti = create_token(
        settings=SETTINGS, subject=world.user.id, token_type=TokenType.REFRESH, jti="never-stored"
    )

    with pytest.raises(TokenRevokedError):
        await world.refresh_use_case().execute(unknown_jti)

    assert world.sessions.revoked_sessions == {}


# --- activeRole on refresh ------------------------------------------------------------------------


async def _refresh_with_claim(world: World, active_role: str | None) -> str | None:
    await world.sessions.store_refresh_token(
        user_id=world.user.id, jti="j", expires_at=_far_future()
    )
    token = create_token(
        settings=SETTINGS,
        subject=world.user.id,
        token_type=TokenType.REFRESH,
        jti="j",
        tenant_id=world.tenant.id,
        active_role=active_role,
    )
    pair = await world.refresh_use_case().execute(token)
    return decode_token(
        settings=SETTINGS, token=pair.access_token, expected_type=TokenType.ACCESS
    ).active_role


async def test_refresh_keeps_an_active_role_that_is_still_assigned() -> None:
    world = await World().setup()
    await world.give_role("editor")
    await world.give_role("viewer")

    assert await _refresh_with_claim(world, "viewer") == "viewer"


async def test_refresh_drops_a_revoked_active_role_and_falls_back_to_an_assigned_one() -> None:
    world = await World().setup()
    await world.give_role("editor")

    assert await _refresh_with_claim(world, "gone-role") == "editor"


async def test_refresh_leaves_no_active_role_when_the_user_has_no_roles() -> None:
    world = await World().setup()

    assert await _refresh_with_claim(world, "gone-role") is None


async def test_refresh_without_a_claim_picks_the_first_assigned_role() -> None:
    world = await World().setup()
    await world.give_role("editor")

    assert await _refresh_with_claim(world, None) == "editor"
