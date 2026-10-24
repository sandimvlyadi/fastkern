from dataclasses import dataclass
from typing import Any

from app.shared.domain.entity import AuditedEntity


@dataclass(kw_only=True)
class Tenant(AuditedEntity):
    name: str
    slug: str
    is_active: bool = True

    def deactivate(self) -> None:
        self.is_active = False

    def activate(self) -> None:
        self.is_active = True

    def audit_snapshot(self) -> dict[str, Any]:
        """State recorded as an audit-log `before`/`after` (camelCase, like the API)."""
        return {"name": self.name, "slug": self.slug, "isActive": self.is_active}
