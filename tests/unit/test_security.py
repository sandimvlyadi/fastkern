from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest

from app.core.config import get_settings
from app.core.security import (
    TokenError,
    TokenType,
    create_token,
    decode_token,
    hash_password,
    verify_password,
)


def test_password_hash_roundtrip() -> None:
    hashed = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", hashed)
    assert not verify_password("wrong password", hashed)


def test_password_hash_is_salted() -> None:
    assert hash_password("same-password") != hash_password("same-password")


def test_token_roundtrip() -> None:
    settings = get_settings()
    user_id = uuid4()
    token = create_token(settings=settings, subject=user_id, token_type=TokenType.ACCESS, jti="abc")
    payload = decode_token(settings=settings, token=token, expected_type=TokenType.ACCESS)
    assert payload.subject == user_id
    assert payload.token_type is TokenType.ACCESS
    assert payload.jti == "abc"


def test_token_wrong_type_rejected() -> None:
    settings = get_settings()
    token = create_token(
        settings=settings, subject=uuid4(), token_type=TokenType.REFRESH, jti="xyz"
    )
    with pytest.raises(TokenError):
        decode_token(settings=settings, token=token, expected_type=TokenType.ACCESS)


def test_token_tampered_signature_rejected() -> None:
    settings = get_settings()
    token = create_token(settings=settings, subject=uuid4(), token_type=TokenType.ACCESS, jti="abc")
    with pytest.raises(TokenError):
        decode_token(settings=settings, token=token + "tampered", expected_type=TokenType.ACCESS)


def test_expired_token_rejected() -> None:
    settings = get_settings().model_copy(update={"access_token_expire_minutes": -1})
    token = create_token(settings=settings, subject=uuid4(), token_type=TokenType.ACCESS, jti="abc")
    with pytest.raises(TokenError, match="expired"):
        decode_token(settings=settings, token=token, expected_type=TokenType.ACCESS)


def test_extra_claims_are_merged_into_token() -> None:
    settings = get_settings()
    token = create_token(
        settings=settings,
        subject=uuid4(),
        token_type=TokenType.ACCESS,
        jti="abc",
        extra_claims={"custom": "value"},
    )
    payload = jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])
    assert payload["custom"] == "value"


def test_decode_token_missing_jti_raises_malformed_error() -> None:
    settings = get_settings()
    now = datetime.now(UTC)
    raw_token = jwt.encode(
        {
            "sub": str(uuid4()),
            "type": TokenType.ACCESS.value,
            "iat": now,
            "exp": now + timedelta(minutes=5),
        },
        settings.secret_key,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(TokenError, match="malformed"):
        decode_token(settings=settings, token=raw_token, expected_type=TokenType.ACCESS)


def test_decode_token_roundtrips_tenant_id_and_active_role() -> None:
    settings = get_settings()
    tenant_id = uuid4()
    token = create_token(
        settings=settings,
        subject=uuid4(),
        token_type=TokenType.ACCESS,
        jti="abc",
        tenant_id=tenant_id,
        active_role="admin",
    )
    payload = decode_token(settings=settings, token=token, expected_type=TokenType.ACCESS)
    assert payload.tenant_id == tenant_id
    assert payload.active_role == "admin"
