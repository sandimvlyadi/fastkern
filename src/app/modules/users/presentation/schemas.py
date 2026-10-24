from datetime import datetime
from functools import cache
from uuid import UUID
from zoneinfo import available_timezones

from pydantic import ConfigDict, Field, field_validator

from app.core.config import get_settings
from app.shared.application.dto import CamelModel

_DEFAULT_TENANT_ID = get_settings().default_tenant_id


class UserResponse(CamelModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                    "tenantId": _DEFAULT_TENANT_ID,
                    "email": "jane.doe@example.com",
                    "fullName": "Jane Doe",
                    "isActive": True,
                    "locale": "en",
                    "timezone": "Asia/Jakarta",
                    "avatarFileId": "9f6a2e3d-1b4c-4a7e-9d2f-6c8b1a3e5f70",
                    "avatarUrl": (
                        "/api/v1/public/files/9f6a2e3d-1b4c-4a7e-9d2f-6c8b1a3e5f70/original"
                    ),
                    "createdAt": "2026-01-15T08:30:00Z",
                    "updatedAt": "2026-03-02T10:12:45Z",
                }
            ]
        }
    )

    id: UUID
    tenant_id: UUID
    email: str
    full_name: str
    is_active: bool
    locale: str
    timezone: str
    avatar_file_id: UUID | None = None
    avatar_url: str | None = None
    created_at: datetime
    updated_at: datetime


def avatar_url(avatar_file_id: UUID | None) -> str | None:
    """`UserResponse.avatar_url`: the public URL of the avatar's original variant."""
    if avatar_file_id is None:
        return None
    return f"{get_settings().api_v1_prefix}/public/files/{avatar_file_id}/original"


@cache
def _known_timezones() -> frozenset[str]:
    return frozenset(available_timezones())


class UpdateUserRequest(CamelModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [{"fullName": "Jane Doe", "locale": "id", "timezone": "Asia/Jakarta"}]
        }
    )

    full_name: str | None = Field(default=None, min_length=1, max_length=255)
    locale: str | None = None
    timezone: str | None = None
    password: str | None = Field(
        default=None,
        min_length=8,
        max_length=128,
        description="Admin-set new password for this user. Setting it immediately revokes "
        "this user's refresh tokens, so no new access token can be issued for them; an "
        "access token already issued stays valid until it expires on its own.",
    )

    # The messages deliberately do not echo the rejected value back.
    @field_validator("locale")
    @classmethod
    def _locale_is_supported(cls, value: str | None) -> str | None:
        supported = get_settings().supported_locales
        if value is not None and value not in supported:
            raise ValueError(f"locale must be one of: {', '.join(supported)}")
        return value

    @field_validator("timezone")
    @classmethod
    def _timezone_is_known(cls, value: str | None) -> str | None:
        if value is not None and value not in _known_timezones():
            raise ValueError("timezone must be a valid IANA timezone, e.g. 'Asia/Jakarta'")
        return value


class ChangePasswordRequest(CamelModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "currentPassword": "<the-account's-current-password>",
                    "newPassword": "<a-new-S3cure-password>",
                }
            ]
        }
    )

    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=8, max_length=128)


class AvatarResponse(CamelModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "avatarFileId": "9f6a2e3d-1b4c-4a7e-9d2f-6c8b1a3e5f70",
                    "avatarUrl": (
                        "/api/v1/public/files/9f6a2e3d-1b4c-4a7e-9d2f-6c8b1a3e5f70/original"
                    ),
                }
            ]
        }
    )

    avatar_file_id: UUID | None
    avatar_url: str | None
