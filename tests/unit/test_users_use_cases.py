from uuid import uuid4

import pytest

from app.core.pagination import OffsetParams
from app.core.security import hash_password, verify_password
from app.modules.users.application.dto import ChangePasswordInput, UpdateUserInput
from app.modules.users.application.use_cases import (
    ActivateUserUseCase,
    ChangeOwnPasswordUseCase,
    DeactivateUserUseCase,
    DeleteUserUseCase,
    GetUserUseCase,
    ListUsersUseCase,
    UpdateUserUseCase,
)
from app.modules.users.domain.entities import User
from app.modules.users.domain.exceptions import (
    CannotModifySelfError,
    IncorrectCurrentPasswordError,
    UserNotFoundError,
)
from tests.fakes import InMemoryTokenSessionStore, InMemoryUserRepository


async def test_get_user_found() -> None:
    users = InMemoryUserRepository()
    user = User(tenant_id=uuid4(), email="a@example.com", hashed_password="h", full_name="A")
    await users.add(user)
    found = await GetUserUseCase(users).execute(user.id)
    assert found.id == user.id


async def test_get_user_not_found_raises() -> None:
    users = InMemoryUserRepository()
    with pytest.raises(UserNotFoundError):
        await GetUserUseCase(users).execute(uuid4())


async def test_list_users_delegates_to_paginate() -> None:
    users = InMemoryUserRepository()
    for i in range(3):
        await users.add(
            User(tenant_id=uuid4(), email=f"u{i}@example.com", hashed_password="h", full_name="A")
        )
    page = await ListUsersUseCase(users).execute(OffsetParams(page=1, page_size=20))
    assert page.total == 3
    assert len(page.items) == 3


async def test_update_user_not_found_raises() -> None:
    users = InMemoryUserRepository()
    with pytest.raises(UserNotFoundError):
        await UpdateUserUseCase(users, InMemoryTokenSessionStore()).execute(
            uuid4(), UpdateUserInput(full_name="New")
        )


async def test_update_user_partial_update_only_changes_given_fields() -> None:
    users = InMemoryUserRepository()
    user = User(
        tenant_id=uuid4(),
        email="a@example.com",
        hashed_password="h",
        full_name="Old Name",
        locale="en",
        timezone="UTC",
    )
    await users.add(user)

    before, after = await UpdateUserUseCase(users, InMemoryTokenSessionStore()).execute(
        user.id, UpdateUserInput(full_name="New Name")
    )

    assert after.full_name == "New Name"
    assert after.locale == "en"
    assert after.timezone == "UTC"
    assert before.full_name == "Old Name"


async def test_update_user_can_change_locale_and_timezone() -> None:
    users = InMemoryUserRepository()
    user = User(
        tenant_id=uuid4(),
        email="a@example.com",
        hashed_password="h",
        full_name="A",
        locale="en",
        timezone="UTC",
    )
    await users.add(user)

    _, after = await UpdateUserUseCase(users, InMemoryTokenSessionStore()).execute(
        user.id, UpdateUserInput(locale="id", timezone="Asia/Jakarta")
    )

    assert after.locale == "id"
    assert after.timezone == "Asia/Jakarta"


async def test_update_user_before_after_are_independent_snapshots() -> None:
    users = InMemoryUserRepository()
    user = User(tenant_id=uuid4(), email="a@example.com", hashed_password="h", full_name="Old")
    await users.add(user)

    before, after = await UpdateUserUseCase(users, InMemoryTokenSessionStore()).execute(
        user.id, UpdateUserInput(full_name="New")
    )

    assert before is not after
    assert before.full_name == "Old"
    assert after.full_name == "New"


async def test_update_user_with_new_password_hashes_it_and_revokes_sessions() -> None:
    users = InMemoryUserRepository()
    user = User(tenant_id=uuid4(), email="a@example.com", hashed_password="h", full_name="A")
    await users.add(user)
    sessions = InMemoryTokenSessionStore()
    sessions.valid.add((user.id, "some-jti"))

    _, after = await UpdateUserUseCase(users, sessions).execute(
        user.id, UpdateUserInput(new_password="BrandNewP4ss!")
    )

    assert after.hashed_password != "h"
    assert verify_password("BrandNewP4ss!", after.hashed_password)
    assert (user.id, "some-jti") not in sessions.valid


async def test_update_user_without_password_does_not_revoke_sessions() -> None:
    users = InMemoryUserRepository()
    user = User(tenant_id=uuid4(), email="a@example.com", hashed_password="h", full_name="A")
    await users.add(user)
    sessions = InMemoryTokenSessionStore()
    sessions.valid.add((user.id, "some-jti"))

    await UpdateUserUseCase(users, sessions).execute(user.id, UpdateUserInput(full_name="B"))

    assert (user.id, "some-jti") in sessions.valid


async def test_change_own_password_success_revokes_sessions() -> None:
    users = InMemoryUserRepository()
    user = User(
        tenant_id=uuid4(),
        email="a@example.com",
        hashed_password=hash_password("OldP4ss!"),
        full_name="A",
    )
    await users.add(user)
    sessions = InMemoryTokenSessionStore()
    sessions.valid.add((user.id, "some-jti"))

    after = await ChangeOwnPasswordUseCase(users, sessions).execute(
        user.id, ChangePasswordInput(current_password="OldP4ss!", new_password="NewP4ss!")
    )

    assert verify_password("NewP4ss!", after.hashed_password)
    assert (user.id, "some-jti") not in sessions.valid


async def test_change_own_password_wrong_current_password_raises_and_changes_nothing() -> None:
    users = InMemoryUserRepository()
    original_hash = hash_password("OldP4ss!")
    user = User(
        tenant_id=uuid4(),
        email="a@example.com",
        hashed_password=original_hash,
        full_name="A",
    )
    await users.add(user)
    sessions = InMemoryTokenSessionStore()
    sessions.valid.add((user.id, "some-jti"))

    with pytest.raises(IncorrectCurrentPasswordError):
        await ChangeOwnPasswordUseCase(users, sessions).execute(
            user.id, ChangePasswordInput(current_password="WrongPass!", new_password="NewP4ss!")
        )

    assert users.users[user.id].hashed_password == original_hash
    assert (user.id, "some-jti") in sessions.valid


async def test_change_own_password_user_not_found_raises() -> None:
    use_case = ChangeOwnPasswordUseCase(InMemoryUserRepository(), InMemoryTokenSessionStore())
    with pytest.raises(UserNotFoundError):
        await use_case.execute(
            uuid4(), ChangePasswordInput(current_password="x", new_password="NewP4ss!")
        )


async def test_deactivate_user_not_found_raises() -> None:
    users = InMemoryUserRepository()
    with pytest.raises(UserNotFoundError):
        await DeactivateUserUseCase(users).execute(uuid4(), uuid4())


async def test_deactivate_user_success() -> None:
    users = InMemoryUserRepository()
    user = User(tenant_id=uuid4(), email="a@example.com", hashed_password="h", full_name="A")
    await users.add(user)

    before, after = await DeactivateUserUseCase(users).execute(user.id, uuid4())

    assert before.is_active is True
    assert after.is_active is False
    assert users.users[user.id].is_active is False


async def test_deactivate_self_is_refused_and_changes_nothing() -> None:
    users = InMemoryUserRepository()
    user = User(tenant_id=uuid4(), email="a@example.com", hashed_password="h", full_name="A")
    await users.add(user)

    with pytest.raises(CannotModifySelfError):
        await DeactivateUserUseCase(users).execute(user.id, user.id)

    assert users.users[user.id].is_active is True


async def test_activate_user_reverses_deactivation() -> None:
    users = InMemoryUserRepository()
    user = User(
        tenant_id=uuid4(),
        email="a@example.com",
        hashed_password="h",
        full_name="A",
        is_active=False,
    )
    await users.add(user)

    before, after = await ActivateUserUseCase(users).execute(user.id)

    assert before.is_active is False
    assert after.is_active is True
    assert users.users[user.id].is_active is True


async def test_activate_is_idempotent_for_an_active_user() -> None:
    users = InMemoryUserRepository()
    user = User(tenant_id=uuid4(), email="a@example.com", hashed_password="h", full_name="A")
    await users.add(user)

    before, after = await ActivateUserUseCase(users).execute(user.id)

    assert before.is_active is True
    assert after.is_active is True


async def test_activate_user_not_found_raises() -> None:
    with pytest.raises(UserNotFoundError):
        await ActivateUserUseCase(InMemoryUserRepository()).execute(uuid4())


async def test_delete_user_returns_before_snapshot_and_deletes() -> None:
    users = InMemoryUserRepository()
    user = User(tenant_id=uuid4(), email="a@example.com", hashed_password="h", full_name="A")
    await users.add(user)

    before = await DeleteUserUseCase(users).execute(user.id, uuid4())

    assert before.email == "a@example.com"
    assert user.id in users.deleted_ids
    assert user.id not in users.users


async def test_delete_self_is_refused_and_deletes_nothing() -> None:
    users = InMemoryUserRepository()
    user = User(tenant_id=uuid4(), email="a@example.com", hashed_password="h", full_name="A")
    await users.add(user)

    with pytest.raises(CannotModifySelfError):
        await DeleteUserUseCase(users).execute(user.id, user.id)

    assert users.deleted_ids == []
    assert user.id in users.users


async def test_delete_user_not_found_raises_and_deletes_nothing() -> None:
    users = InMemoryUserRepository()
    missing_id = uuid4()

    with pytest.raises(UserNotFoundError):
        await DeleteUserUseCase(users).execute(missing_id, uuid4())

    assert users.deleted_ids == []
