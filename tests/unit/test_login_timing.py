"""Unknown-email logins must cost the same argon2 work as wrong-password ones."""

from uuid import uuid4

import pytest

import app.core.security as security
from app.core.config import get_settings
from app.core.security import hash_password, verify_password_dummy
from app.modules.auth.application.dto import LoginInput
from app.modules.auth.application.use_cases import LoginUseCase
from app.modules.auth.domain.exceptions import InvalidCredentialsError
from app.modules.users.domain.entities import User
from tests.fakes import (
    InMemoryTenantRepository,
    InMemoryTokenSessionStore,
    InMemoryUserRepository,
    InMemoryUserRoleRepository,
)


def _use_case(users: InMemoryUserRepository) -> LoginUseCase:
    return LoginUseCase(
        users,
        InMemoryUserRoleRepository(),
        get_settings(),
        InMemoryTokenSessionStore(),
        InMemoryTenantRepository(),
    )


def test_verify_password_dummy_runs_the_real_hasher(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []
    real = security._hasher

    class _SpyHasher:
        # PasswordHasher is slotted (read-only attributes), so wrap it instead of patching it.
        def hash(self, password: str) -> str:
            return real.hash(password)

        def verify(self, hashed: str, plain: str) -> bool:
            calls.append((hashed, plain))
            return real.verify(hashed, plain)

    monkeypatch.setattr(security, "_hasher", _SpyHasher())

    assert verify_password_dummy("whatever") is None

    assert len(calls) == 1
    hashed, plain = calls[0]
    assert plain == "whatever"
    assert hashed.startswith("$argon2")  # a real hash with the hasher's own parameters


def test_dummy_hash_is_computed_once_and_shared() -> None:
    assert security._dummy_hash() is security._dummy_hash()


def test_verify_password_dummy_never_raises_or_returns_a_verdict() -> None:
    assert verify_password_dummy("") is None
    assert verify_password_dummy("x" * 500) is None


async def test_login_with_unknown_email_pays_for_a_dummy_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []
    monkeypatch.setattr("app.modules.auth.application.use_cases.verify_password_dummy", seen.append)

    with pytest.raises(InvalidCredentialsError):
        await _use_case(InMemoryUserRepository()).execute(
            LoginInput(tenant_id=uuid4(), email="ghost@example.com", password="guess")
        )

    assert seen == ["guess"]


async def test_login_with_known_email_does_not_use_the_dummy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    users = InMemoryUserRepository()
    user = User(
        tenant_id=uuid4(),
        email="real@example.com",
        hashed_password=hash_password("right-password"),
        full_name="Real",
    )
    await users.add(user)
    seen: list[str] = []
    monkeypatch.setattr("app.modules.auth.application.use_cases.verify_password_dummy", seen.append)

    with pytest.raises(InvalidCredentialsError):
        await _use_case(users).execute(
            LoginInput(tenant_id=user.tenant_id, email=user.email, password="wrong-password")
        )

    assert seen == []
