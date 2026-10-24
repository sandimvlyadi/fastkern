from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class RegisterInput:
    tenant_id: UUID
    email: str
    password: str
    full_name: str


@dataclass(frozen=True, slots=True)
class LoginInput:
    tenant_id: UUID
    email: str
    password: str


@dataclass(frozen=True, slots=True)
class TokenPair:
    access_token: str
    refresh_token: str
    token_type: str = "bearer"  # noqa: S105 - not a secret, it's the OAuth2 scheme name
