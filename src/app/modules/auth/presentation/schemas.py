from uuid import UUID

from pydantic import ConfigDict, EmailStr, Field, field_validator

from app.core.config import get_settings
from app.modules.users.domain.email import normalize_email
from app.shared.application.dto import CamelModel

_DEFAULT_TENANT_ID = get_settings().default_tenant_id


class RegisterRequest(CamelModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "tenantId": _DEFAULT_TENANT_ID,
                    "email": "jane.doe@example.com",
                    "password": "S3curePass!",
                    "fullName": "Jane Doe",
                }
            ]
        }
    )

    tenant_id: UUID
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=1, max_length=255)

    @field_validator("email")
    @classmethod
    def _canonical_email(cls, value: str) -> str:
        return normalize_email(value)


class LoginRequest(CamelModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "tenantId": _DEFAULT_TENANT_ID,
                    "email": "jane.doe@example.com",
                    "password": "S3curePass!",
                }
            ]
        }
    )

    tenant_id: UUID
    email: EmailStr
    password: str

    @field_validator("email")
    @classmethod
    def _canonical_email(cls, value: str) -> str:
        return normalize_email(value)


class RefreshRequest(CamelModel):
    # Placeholder, deliberately not JWT-shaped (see TokenResponse below).
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"refreshToken": "<refresh-token-returned-by-login>"}]}
    )

    refresh_token: str


class LogoutRequest(CamelModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"refreshToken": "<refresh-token-returned-by-login>"}]}
    )

    refresh_token: str


class SwitchRoleRequest(CamelModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"roleName": "admin"}]})

    role_name: str


class TokenResponse(CamelModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    # Deliberately not JWT-shaped: a realistic `eyJ…` example would trip
                    # gitleaks' `jwt` rule on every commit.
                    "accessToken": "<access-token-returned-by-this-endpoint>",
                    "refreshToken": "<refresh-token-returned-by-this-endpoint>",
                    "tokenType": "bearer",
                }
            ]
        }
    )

    access_token: str
    refresh_token: str
    token_type: str = "bearer"  # noqa: S105 - not a secret, it's the OAuth2 scheme name


class UserSummaryResponse(CamelModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                    "email": "jane.doe@example.com",
                    "fullName": "Jane Doe",
                    "isActive": True,
                    "avatarFileId": None,
                    "avatarUrl": None,
                }
            ]
        }
    )

    id: UUID
    email: str
    full_name: str
    is_active: bool
    avatar_file_id: UUID | None = None
    avatar_url: str | None = None


class MeResponse(UserSummaryResponse):
    """`UserSummaryResponse` plus the caller's currently active role — the
    one field `GET /auth/me` adds on top of the summary (see router.py)."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                    "email": "jane.doe@example.com",
                    "fullName": "Jane Doe",
                    "isActive": True,
                    "activeRole": "editor",
                }
            ]
        }
    )

    active_role: str | None = None


class MyRoleResponse(CamelModel):
    """One role assigned to the caller — returned by `GET /auth/me/roles`
    (self-service; no `roles:read` permission needed, unlike
    `GET /roles/users/{user_id}` which is admin-only)."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": "b3f1c9de-8f2a-4a6e-9b1a-6e2d4c8f0a11",
                    "name": "editor",
                    "description": "Can create and edit content but not manage users.",
                    "permissions": ["users:read", "audit_log:read"],
                    "isDefault": True,
                }
            ]
        }
    )

    id: UUID
    name: str
    description: str
    permissions: list[str]
    is_default: bool
