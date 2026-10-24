"""Domain entities for the `posts` module. Plain dataclasses only — no
FastAPI/SQLAlchemy imports (enforced by import-linter).

`Post` deliberately does NOT extend the shared `Entity`/`AuditedEntity`/
`TenantScopedEntity` base classes: its id is owned by the external
JSONPlaceholder API (an int), not server-generated (those bases default
`id` to a `UUID` via `uuid7()`), and there is no tenant/soft-delete
concept for an external resource.
"""

from dataclasses import dataclass
from typing import Any


@dataclass(kw_only=True)
class Post:
    id: int
    user_id: int
    title: str
    body: str

    def audit_snapshot(self) -> dict[str, Any]:
        """State recorded as an audit-log `before`/`after` (camelCase, like the API)."""
        return {"userId": self.user_id, "title": self.title, "body": self.body}
