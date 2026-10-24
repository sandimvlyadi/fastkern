"""Use cases (application services) for the users module.

Each use case takes its dependencies (ports) via constructor injection and does
exactly one thing — this is the layer audit-logged actions and authorization checks
hook into.
"""

import contextlib
from dataclasses import replace
from uuid import UUID

from app.core.pagination import OffsetPage, OffsetParams
from app.core.security import hash_password, verify_password
from app.modules.auth.domain.ports import TokenSessionStorePort
from app.modules.files.application.use_cases import (
    DeleteFileUseCase,
    UploadPolicy,
    UploadSingleFileUseCase,
)
from app.modules.files.domain.exceptions import FileAssetNotFoundError, FileNotAvailableError
from app.modules.files.domain.ports import (
    ChunkRepositoryPort,
    FileRepositoryPort,
    ObjectStoragePort,
    UploadRepositoryPort,
)
from app.modules.files.domain.state import STALE_FILE_STATUSES, FileStatus
from app.modules.files.domain.value_objects import Visibility
from app.modules.users.application.dto import (
    ChangePasswordInput,
    UpdateAvatarInput,
    UpdateUserInput,
)
from app.modules.users.domain.entities import User
from app.modules.users.domain.exceptions import (
    AvatarChangeInProgressError,
    CannotModifySelfError,
    IncorrectCurrentPasswordError,
    UserHasNoAvatarError,
    UserNotFoundError,
)
from app.modules.users.domain.ports import UserRepositoryPort


class GetUserUseCase:
    def __init__(self, users: UserRepositoryPort) -> None:
        self._users = users

    async def execute(self, user_id: UUID) -> User:
        user = await self._users.get_by_id(user_id)
        if user is None:
            raise UserNotFoundError(user_id=str(user_id))
        return user


class ListUsersUseCase:
    def __init__(self, users: UserRepositoryPort) -> None:
        self._users = users

    async def execute(self, params: OffsetParams) -> OffsetPage[User]:
        return await self._users.paginate(params)


class UpdateUserUseCase:
    def __init__(self, users: UserRepositoryPort, sessions: TokenSessionStorePort) -> None:
        self._users = users
        self._sessions = sessions

    async def execute(self, user_id: UUID, data: UpdateUserInput) -> tuple[User, User]:
        """Returns (before, after) so callers building an audit-log diff
        don't need a second fetch of the same row."""
        user = await self._users.get_by_id(user_id)
        if user is None:
            raise UserNotFoundError(user_id=str(user_id))
        before = replace(user)
        if data.full_name is not None:
            user.full_name = data.full_name
        if data.locale is not None:
            user.locale = data.locale
        if data.timezone is not None:
            user.timezone = data.timezone
        if data.new_password is not None:
            user.change_password(hash_password(data.new_password))
        after = await self._users.save(user)
        if data.new_password is not None:
            # Revokes the target's stored refresh tokens so none of them can mint a new access
            # token. An access token already issued is unaffected and stays valid until it
            # expires on its own (<= settings.access_token_expire_minutes) — there is no
            # session-id-level revocation here, only refresh-token-level.
            await self._sessions.revoke_all_for_user(user_id=user.id)
        return before, after


class ChangeOwnPasswordUseCase:
    """Self-service password change: the caller proves they know the current password (unlike
    the admin path in `UpdateUserUseCase`, which can set a password outright)."""

    def __init__(self, users: UserRepositoryPort, sessions: TokenSessionStorePort) -> None:
        self._users = users
        self._sessions = sessions

    async def execute(self, user_id: UUID, data: ChangePasswordInput) -> User:
        user = await self._users.get_by_id(user_id)
        if user is None:
            raise UserNotFoundError(user_id=str(user_id))
        if not verify_password(data.current_password, user.hashed_password):
            raise IncorrectCurrentPasswordError()
        user.change_password(hash_password(data.new_password))
        after = await self._users.save(user)
        # Same guarantee (and same limit) as the admin-reset path above: revokes stored refresh
        # tokens, not any access token already issued.
        await self._sessions.revoke_all_for_user(user_id=user.id)
        return after


class DeactivateUserUseCase:
    def __init__(self, users: UserRepositoryPort) -> None:
        self._users = users

    async def execute(self, user_id: UUID, caller_id: UUID) -> tuple[User, User]:
        """Returns (before, after) for the caller's audit-log diff, same
        shape as `UpdateUserUseCase`. Refuses to deactivate the caller
        themselves: the only admin could otherwise lock everyone out."""
        if user_id == caller_id:
            raise CannotModifySelfError()
        user = await self._users.get_by_id(user_id)
        if user is None:
            raise UserNotFoundError(user_id=str(user_id))
        before = replace(user)
        user.deactivate()
        after = await self._users.save(user)
        return before, after


class ActivateUserUseCase:
    def __init__(self, users: UserRepositoryPort) -> None:
        self._users = users

    async def execute(self, user_id: UUID) -> tuple[User, User]:
        """Reverses `DeactivateUserUseCase`. Idempotent (activating an active user is a no-op
        that still returns the pair). Returns (before, after) for the audit-log diff."""
        user = await self._users.get_by_id(user_id)
        if user is None:
            raise UserNotFoundError(user_id=str(user_id))
        before = replace(user)
        user.activate()
        after = await self._users.save(user)
        return before, after


class DeleteUserUseCase:
    def __init__(self, users: UserRepositoryPort) -> None:
        self._users = users

    async def execute(self, user_id: UUID, caller_id: UUID) -> User:
        """Soft-deletes the user and returns them as they were, for the audit log. Refuses
        to delete the caller themselves."""
        if user_id == caller_id:
            raise CannotModifySelfError()
        user = await self._users.get_by_id(user_id)
        if user is None:
            raise UserNotFoundError(user_id=str(user_id))
        before = replace(user)
        await self._users.delete(user_id)
        return before


class UpdateAvatarUseCase:
    """Uploads a new avatar for `target_user_id` and swaps it in, deleting the previous one
    (best-effort) when it is safely replaceable.

    Reuses the files module's own `UploadSingleFileUseCase`/`DeleteFileUseCase` rather than
    reimplementing upload race-safety/quota logic."""

    def __init__(
        self,
        users: UserRepositoryPort,
        uploads: UploadRepositoryPort,
        chunks: ChunkRepositoryPort,
        files: FileRepositoryPort,
        storage: ObjectStoragePort,
        policy: UploadPolicy,
    ) -> None:
        self._users = users
        self._uploads = uploads
        self._chunks = chunks
        self._files = files
        self._storage = storage
        self._policy = policy

    async def execute(self, data: UpdateAvatarInput) -> tuple[User, UUID, UUID]:
        """Returns (updated_user, upload_id, file_id) — the router dispatches both
        `files.process_upload` and `users.finalize_avatar` with these ids."""
        user = await self._users.get_by_id(data.target_user_id)
        if user is None:
            raise UserNotFoundError(user_id=str(data.target_user_id))

        previous_avatar_id = user.avatar_file_id
        if previous_avatar_id is not None:
            previous = await self._files.get_owned_file(previous_avatar_id)
            if previous is not None and previous.status in STALE_FILE_STATUSES:
                raise AvatarChangeInProgressError()

        upload_id, file_id, _ = await UploadSingleFileUseCase(
            self._uploads, self._chunks, self._files, self._storage, self._policy
        ).execute(
            filename=data.filename,
            size=data.size,
            sha256=data.sha256,
            visibility=Visibility.PUBLIC,  # avatars are always publicly servable
            body=data.body,
            tenant_id=data.tenant_id,
            owner_id=data.target_user_id,  # the file belongs to the profile it pictures
        )
        user.avatar_file_id = file_id
        updated = await self._users.save(user)

        if previous_avatar_id is not None:
            # Already gone, or not yet AVAILABLE despite the guard above (race): the
            # files.cleanup beat job sweeps it once it goes stale.
            with contextlib.suppress(FileAssetNotFoundError, FileNotAvailableError):
                await DeleteFileUseCase(self._files).execute(
                    previous_avatar_id, data.caller_id, can_delete_all=True
                )
        return updated, upload_id, file_id


class RemoveAvatarUseCase:
    def __init__(self, users: UserRepositoryPort, files: FileRepositoryPort) -> None:
        self._users = users
        self._files = files

    async def execute(self, target_user_id: UUID, caller_id: UUID) -> User:
        """`caller_id` is the authenticated actor — the same as `target_user_id` for
        self-service, distinct when an admin removes another user's avatar — and is what
        `DeleteFileUseCase` records as the file's `deleted_by`."""
        user = await self._users.get_by_id(target_user_id)
        if user is None:
            raise UserNotFoundError(user_id=str(target_user_id))
        if user.avatar_file_id is None:
            raise UserHasNoAvatarError()

        avatar_id = user.avatar_file_id
        user.avatar_file_id = None
        updated = await self._users.save(user)

        asset = await self._files.get_owned_file(avatar_id)
        if asset is not None and asset.status is FileStatus.AVAILABLE:
            await DeleteFileUseCase(self._files).execute(avatar_id, caller_id, can_delete_all=True)
        # Else: not AVAILABLE (still processing, or already failed/deleted) — nothing to
        # delete now; files.cleanup sweeps a stuck UPLOADED/SCANNING/PROCESSING row once stale.
        return updated
