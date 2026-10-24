from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class UpdateUserInput:
    full_name: str | None = None
    locale: str | None = None
    timezone: str | None = None
    new_password: str | None = None
    """Admin-set password (plain text, hashed by the use case) via `PATCH /users/{id}`.
    Unlike the other fields, setting this also revokes the target user's sessions."""


@dataclass(frozen=True, slots=True)
class ChangePasswordInput:
    current_password: str
    new_password: str


@dataclass(frozen=True, slots=True)
class UpdateAvatarInput:
    target_user_id: UUID
    caller_id: UUID
    """The authenticated actor performing the change — the same as `target_user_id` for
    self-service, but distinct when an admin acts on another user's avatar. Recorded as the
    `deleted_by` actor when the previous avatar is cleaned up."""
    tenant_id: UUID
    filename: str
    size: int
    sha256: str | None
    body: AsyncIterator[bytes]
