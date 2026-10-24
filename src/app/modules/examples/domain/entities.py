from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from app.shared.domain.entity import Entity, TenantScopedEntity


@dataclass(kw_only=True)
class ExampleItem(Entity):
    label: str


@dataclass(kw_only=True)
class Example(TenantScopedEntity):
    name: str
    description: str = ""
    master_example_id: UUID | None = None
    items: list[ExampleItem] = field(default_factory=list)

    def audit_snapshot(self) -> dict[str, Any]:
        """State recorded as an audit-log `before`/`after` (camelCase, like the API)."""
        return {
            "name": self.name,
            "description": self.description,
            "masterExampleId": str(self.master_example_id) if self.master_example_id else None,
            "items": sorted(item.label for item in self.items),
        }
