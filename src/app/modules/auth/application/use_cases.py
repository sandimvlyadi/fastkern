"""Auth use cases.

These orchestrate `users`, `roles`, and this module's own token/session
port. Cross-module coupling here happens strictly through domain ports
(interfaces) — never by importing another module's infrastructure — so
each module's persistence stays swappable in isolation.
"""

import uuid
from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.security import (
    TokenError,
    TokenPayload,
    TokenType,
    create_token,
    decode_token,
    hash_password,
    verify_password,
    verify_password_dummy,
)
from app.modules.auth.application.dto import LoginInput, RegisterInput, TokenPair
from app.modules.auth.domain.exceptions import (
    AccountDisabledError,
    InvalidCredentialsError,
    InvalidTokenError,
    TokenRevokedError,
)
from app.modules.auth.domain.ports import TokenSessionStorePort
from app.modules.roles.application.use_cases import SwitchActiveRoleUseCase
from app.modules.roles.domain.ports import RoleRepositoryPort, UserRoleRepositoryPort
from app.modules.tenants.domain.exceptions import TenantInactiveError, TenantNotFoundError
from app.modules.tenants.domain.ports import TenantRepositoryPort
from app.modules.users.domain.email import normalize_email
from app.modules.users.domain.entities import User
from app.modules.users.domain.exceptions import EmailAlreadyRegisteredError
from app.modules.users.domain.ports import UserRepositoryPort

logger = get_logger(__name__)

DEFAULT_SELF_REGISTER_ROLE = "user"


class _TokenIssuer:
    """Shared helper: mint an access+refresh pair for a user, recording the
    refresh token's jti in the session store so it can be revoked later."""

    def __init__(self, settings: Settings, sessions: TokenSessionStorePort) -> None:
        self._settings = settings
        self._sessions = sessions

    async def issue(
        self, user: User, *, active_role: str | None, session_id: str | None = None
    ) -> TokenPair:
        """`session_id` continues an existing login session (refresh rotation, role switch);
        None starts a new one (login) — or upgrades a pre-session token on refresh."""
        access_jti = str(uuid.uuid4())
        refresh_jti = str(uuid.uuid4())
        sid = session_id or str(uuid.uuid4())

        access_token = create_token(
            settings=self._settings,
            subject=user.id,
            token_type=TokenType.ACCESS,
            jti=access_jti,
            tenant_id=user.tenant_id,
            active_role=active_role,
            session_id=sid,
        )
        refresh_token = create_token(
            settings=self._settings,
            subject=user.id,
            token_type=TokenType.REFRESH,
            jti=refresh_jti,
            tenant_id=user.tenant_id,
            active_role=active_role,
            session_id=sid,
        )

        expires_at = datetime.now(UTC) + timedelta(
            minutes=self._settings.refresh_token_expire_minutes
        )
        await self._sessions.store_refresh_token(
            user_id=user.id, jti=refresh_jti, expires_at=expires_at
        )

        return TokenPair(access_token=access_token, refresh_token=refresh_token)


async def _ensure_tenant_active(tenants: TenantRepositoryPort, tenant_id: UUID) -> None:
    """Suspending a tenant (`is_active=False`) must lock out its existing
    users too, not just block new signups."""
    tenant = await tenants.get_by_id(tenant_id)
    if tenant is None or not tenant.is_active:
        raise TenantInactiveError(tenant_id=str(tenant_id))


class RegisterUseCase:
    def __init__(
        self,
        users: UserRepositoryPort,
        roles: RoleRepositoryPort,
        user_roles: UserRoleRepositoryPort,
        tenants: TenantRepositoryPort,
    ) -> None:
        self._users = users
        self._roles = roles
        self._user_roles = user_roles
        self._tenants = tenants

    async def execute(self, data: RegisterInput) -> User:
        # The tenant_id is client-supplied and unauthenticated: never trust it
        # until it's confirmed to be a real, active tenant. An inactive tenant
        # is reported exactly like a missing one so this unauthenticated
        # endpoint can't be used to probe which tenant ids exist.
        tenant = await self._tenants.get_by_id(data.tenant_id)
        if tenant is None or not tenant.is_active:
            raise TenantNotFoundError(tenant_id=str(data.tenant_id))
        # Also normalised here (not only in the request schema): defense in depth for any other
        # caller, and the DB refuses a non-lowercase email outright (ck_sys_users_email_lower).
        email = normalize_email(data.email)
        if await self._users.get_by_email(email, tenant_id=data.tenant_id) is not None:
            raise EmailAlreadyRegisteredError(email=email)
        user = User(
            tenant_id=data.tenant_id,
            email=email,
            hashed_password=hash_password(data.password),
            full_name=data.full_name,
        )
        user = await self._users.add(user)

        # No JWT exists yet at this point in the request, so there is no
        # ambient tenant context (mirrors LoginUseCase below) — pass the
        # client-supplied tenant_id explicitly to the tenant-scoped role
        # lookup/assignment rather than mutating global request context.
        role = await self._roles.get_by_name(DEFAULT_SELF_REGISTER_ROLE, tenant_id=data.tenant_id)
        if role is not None:
            await self._user_roles.assign(user.id, role.id, tenant_id=data.tenant_id)
        else:
            # A tenant that hasn't been seeded with a "user" role yet must
            # not block signups — this is a config gap to fix, not a
            # reason to fail registration.
            logger.warning(
                "default_role_missing", role=DEFAULT_SELF_REGISTER_ROLE, tenant_id=data.tenant_id
            )
        return user


class LoginUseCase:
    def __init__(
        self,
        users: UserRepositoryPort,
        user_roles: UserRoleRepositoryPort,
        settings: Settings,
        sessions: TokenSessionStorePort,
        tenants: TenantRepositoryPort,
    ) -> None:
        self._users = users
        self._user_roles = user_roles
        self._tenants = tenants
        self._issuer = _TokenIssuer(settings, sessions)

    async def execute(self, data: LoginInput) -> tuple[User, TokenPair]:
        user = await self._users.get_by_email(normalize_email(data.email), tenant_id=data.tenant_id)
        if user is None:
            # Burn the same argon2 time a real check would, so response time doesn't reveal
            # whether the email is registered.
            verify_password_dummy(data.password)
            raise InvalidCredentialsError()
        if not verify_password(data.password, user.hashed_password):
            raise InvalidCredentialsError()
        if not user.is_active:
            raise AccountDisabledError()
        # Checked only once the credentials are proven, so an unauthenticated
        # caller can't learn whether a tenant is suspended.
        await _ensure_tenant_active(self._tenants, user.tenant_id)

        # No JWT exists yet at this point in the request, so there is no
        # ambient tenant context — pass the resolved user's own tenant_id
        # explicitly (not the client-supplied input) rather than mutating
        # global request context.
        default_role = await self._user_roles.get_default_role_for_user(
            user.id, tenant_id=user.tenant_id
        )
        active_role: str | None
        if default_role is not None:
            active_role = default_role.name
        else:
            roles = await self._user_roles.list_roles_for_user(user.id, tenant_id=user.tenant_id)
            active_role = roles[0].name if roles else None
        tokens = await self._issuer.issue(user, active_role=active_role)
        return user, tokens


def _session_revocation_ttl_seconds(settings: Settings) -> int:
    """How long a revoked session must stay denied: as long as its longest-lived token (the
    refresh token) could still be presented. `switch-role` mints a new refresh token without
    revoking the previous one, so a shorter TTL would let that older token revive the session."""
    return settings.refresh_token_expire_minutes * 60


class RefreshTokenUseCase:
    def __init__(
        self,
        users: UserRepositoryPort,
        settings: Settings,
        sessions: TokenSessionStorePort,
        tenants: TenantRepositoryPort,
        user_roles: UserRoleRepositoryPort,
    ) -> None:
        self._users = users
        self._settings = settings
        self._sessions = sessions
        self._tenants = tenants
        self._user_roles = user_roles
        self._issuer = _TokenIssuer(settings, sessions)

    async def execute(self, refresh_token: str) -> TokenPair:
        try:
            payload = decode_token(
                settings=self._settings, token=refresh_token, expected_type=TokenType.REFRESH
            )
        except TokenError as exc:
            raise InvalidTokenError() from exc

        # A session that was logged out or flagged for reuse stays dead even if some refresh
        # token of it is still in the store (see `_session_revocation_ttl_seconds`).
        if payload.session_id and await self._sessions.is_session_revoked(
            session_id=payload.session_id
        ):
            raise TokenRevokedError()

        # Atomic check-and-revoke: a separate is_refresh_token_valid()
        # check followed by a later revoke_refresh_token() would leave a
        # window where two concurrent requests presenting the same token
        # both pass the check before either revokes it, defeating reuse
        # detection (both would mint a valid new pair from one token).
        if not await self._sessions.consume_refresh_token(user_id=payload.subject, jti=payload.jti):
            # A refresh token whose signature and expiry check out but which is no longer in the
            # store was already used (or logged out). Presenting it again is the signature of a
            # stolen token, and we can't tell which party holds the newer one — so end the whole
            # session (OAuth 2.0 Security BCP, refresh-token rotation). A foreign or expired
            # token never gets here: it already failed decode above, so it revokes nothing.
            if payload.session_id:
                await self._sessions.revoke_session(
                    session_id=payload.session_id,
                    ttl_seconds=_session_revocation_ttl_seconds(self._settings),
                )
                logger.warning(
                    "refresh_token_reuse_detected",
                    user_id=str(payload.subject),
                    session_id=payload.session_id,
                )
            raise TokenRevokedError()

        # No get_current_user dependency runs for /auth/refresh (only the
        # refresh token itself, not an access token, is presented), so no
        # ambient tenant context exists yet — this trusts the signed
        # token's subject the same way core.di.get_current_user does.
        user = await self._users.get_by_id(payload.subject, tenant_scoped=False)
        if user is None or not user.is_active:
            raise InvalidTokenError()
        await _ensure_tenant_active(self._tenants, user.tenant_id)

        # The claim can outlive the assignment (role revoked or deleted since the token was
        # minted): only carry it forward if it is still assigned, else fall back to the first
        # assigned role, exactly as login picks one.
        assigned = [
            role.name
            for role in await self._user_roles.list_roles_for_user(
                user.id, tenant_id=user.tenant_id
            )
        ]
        active_role = payload.active_role if payload.active_role in assigned else None
        if active_role is None and assigned:
            default_role = await self._user_roles.get_default_role_for_user(
                user.id, tenant_id=user.tenant_id
            )
            active_role = default_role.name if default_role is not None else assigned[0]

        return await self._issuer.issue(
            user, active_role=active_role, session_id=payload.session_id
        )


class LogoutUseCase:
    def __init__(self, settings: Settings, sessions: TokenSessionStorePort) -> None:
        self._settings = settings
        self._sessions = sessions

    async def execute(self, refresh_token: str) -> TokenPayload | None:
        """Returns the decoded payload on success (so the caller can audit
        the logout as the right actor/tenant), or None for an already
        invalid/expired token — logout is a no-op in that case, not an
        error, so callers should skip auditing rather than fail."""
        try:
            payload = decode_token(
                settings=self._settings, token=refresh_token, expected_type=TokenType.REFRESH
            )
        except TokenError:
            return None
        await self._sessions.revoke_refresh_token(user_id=payload.subject, jti=payload.jti)
        # Also kill the access token(s) of this session, which are otherwise valid until they
        # expire. Only this session: the user's other logins carry a different `sid`.
        if payload.session_id:
            await self._sessions.revoke_session(
                session_id=payload.session_id,
                ttl_seconds=_session_revocation_ttl_seconds(self._settings),
            )
        return payload


class SwitchRoleUseCase:
    """Validates the target role is assigned to the user, then reissues a
    fresh token pair carrying the new `active_role` claim."""

    def __init__(
        self,
        users: UserRepositoryPort,
        settings: Settings,
        sessions: TokenSessionStorePort,
        switch_role: SwitchActiveRoleUseCase,
    ) -> None:
        self._users = users
        self._issuer = _TokenIssuer(settings, sessions)
        self._switch_role = switch_role

    async def execute(
        self, user_id: UUID, role_name: str, *, session_id: str | None = None
    ) -> TokenPair:
        """`session_id` is the caller's current session: the new pair stays part of it, so
        logging out (or a reuse revocation) still covers tokens minted here."""
        role = await self._switch_role.execute(user_id, role_name)
        user = await self._users.get_by_id(user_id)
        assert user is not None  # guaranteed: role lookup above already validated the user exists
        return await self._issuer.issue(user, active_role=role.name, session_id=session_id)
