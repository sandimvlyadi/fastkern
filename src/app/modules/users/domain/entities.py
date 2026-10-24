from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.shared.domain.entity import TenantScopedEntity


@dataclass(kw_only=True)
class User(TenantScopedEntity):
    email: str
    hashed_password: str
    full_name: str
    is_active: bool = True
    locale: str = "en"
    timezone: str = "UTC"
    avatar_file_id: UUID | None = None

    def deactivate(self) -> None:
        self.is_active = False

    def activate(self) -> None:
        self.is_active = True

    def change_password(self, hashed_password: str) -> None:
        self.hashed_password = hashed_password

    def audit_snapshot(self) -> dict[str, Any]:
        """State recorded as an audit-log `before`/`after`. Explicit field
        list (camelCase, like the API) so `hashed_password` can never leak
        into the audit trail."""
        return {
            "email": self.email,
            "fullName": self.full_name,
            "isActive": self.is_active,
            "locale": self.locale,
            "timezone": self.timezone,
        }
