"""Password hashing and JWT token issuing/verification.

Kept framework-agnostic on purpose (no FastAPI imports) so it stays usable
from the `auth` module's domain/application layers as well as from Celery
tasks and CLI scripts.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import lru_cache
from typing import Any
from uuid import UUID

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.core.config import Settings

_hasher = PasswordHasher()


def hash_password(plain_password: str) -> str:
    return _hasher.hash(plain_password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return _hasher.verify(hashed_password, plain_password)
    except VerifyMismatchError:
        return False


@lru_cache
def _dummy_hash() -> str:
    # Same hasher (so same argon2 cost parameters) as every real hash; built lazily, once.
    return _hasher.hash("timing-equaliser-not-a-real-password")


def verify_password_dummy(plain_password: str) -> None:
    """Spend the same argon2 time as `verify_password` against a real hash.

    Call it on the "no such user" path of a login: without it that path returns in ~3 ms while
    a wrong password takes ~60 ms, which lets a client enumerate registered emails by timing.
    The result is deliberately discarded."""
    verify_password(plain_password, _dummy_hash())


class TokenType(StrEnum):
    ACCESS = "access"
    REFRESH = "refresh"


@dataclass(frozen=True, slots=True)
class TokenPayload:
    subject: UUID
    token_type: TokenType
    tenant_id: UUID | None
    active_role: str | None
    jti: str
    expires_at: datetime
    session_id: str | None = None
    """`sid` claim: identifies the login session a token pair belongs to (constant across refresh
    rotation), so a whole session can be revoked at once. None on tokens minted before sessions
    existed, which stay valid until they expire."""


class TokenError(Exception):
    """Raised for any invalid/expired/malformed token."""


def create_token(
    *,
    settings: Settings,
    subject: UUID,
    token_type: TokenType,
    jti: str,
    tenant_id: UUID | None = None,
    active_role: str | None = None,
    session_id: str | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    now = datetime.now(UTC)
    expire_minutes = (
        settings.access_token_expire_minutes
        if token_type is TokenType.ACCESS
        else settings.refresh_token_expire_minutes
    )
    expires_at = now + timedelta(minutes=expire_minutes)

    claims: dict[str, Any] = {
        "sub": str(subject),
        "type": token_type.value,
        "jti": jti,
        "iat": now,
        "exp": expires_at,
    }
    if tenant_id is not None:
        claims["tenant_id"] = str(tenant_id)
    if active_role is not None:
        claims["active_role"] = active_role
    if session_id is not None:
        claims["sid"] = session_id
    if extra_claims:
        claims.update(extra_claims)

    return jwt.encode(claims, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_token(*, settings: Settings, token: str, expected_type: TokenType) -> TokenPayload:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("Token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("Token is invalid") from exc

    if payload.get("type") != expected_type.value:
        raise TokenError(f"Expected a {expected_type.value} token")

    try:
        return TokenPayload(
            subject=UUID(payload["sub"]),
            token_type=TokenType(payload["type"]),
            tenant_id=UUID(payload["tenant_id"]) if payload.get("tenant_id") else None,
            active_role=payload.get("active_role"),
            jti=payload["jti"],
            expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
            session_id=str(payload["sid"]) if payload.get("sid") else None,
        )
    except (KeyError, ValueError) as exc:
        raise TokenError("Token payload is malformed") from exc
