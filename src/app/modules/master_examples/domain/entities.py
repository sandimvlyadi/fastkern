from dataclasses import dataclass
from typing import Any

from app.shared.domain.entity import TenantScopedEntity


@dataclass(kw_only=True)
class MasterExample(TenantScopedEntity):
    name: str
    description: str = ""

    def audit_snapshot(self) -> dict[str, Any]:
        """State recorded as an audit-log `before`/`after` (camelCase, like the API)."""
        return {"name": self.name, "description": self.description}
