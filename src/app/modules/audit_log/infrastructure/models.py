import uuid
from typing import Any

from sqlalchemy import Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.infrastructure.orm_base import (
    ORDER_TENANT,
    Base,
    CreatedAtMixin,
    JsonB,
    UUIDPrimaryKeyMixin,
)


class AuditLogModel(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Append-only: no updated_at / actor / soft-delete columns on purpose —
    rows are never mutated after insert (`actor_id` below is the audited
    event's subject, not a row-maintenance column). `tenant_id` is nullable
    and has no FK to allow platform-level events (e.g. tenant provisioning)
    that precede any tenant context, and so a log line outlives its tenant.

    Indexes follow the actual filters in `AuditLogRepository.paginate()`
    (always tenant-scoped, newest first, optionally by resource type or
    actor) plus a bare `created_at` for retention purges across tenants.
    """

    __tablename__ = "sys_audit_logs"
    __table_args__ = (
        Index("ix_sys_audit_logs_tenant_id_created_at", "tenant_id", "created_at"),
        Index("ix_sys_audit_logs_tenant_id_actor_id", "tenant_id", "actor_id"),
        Index(
            "ix_sys_audit_logs_tenant_id_resource_type_resource_id",
            "tenant_id",
            "resource_type",
            "resource_id",
        ),
        Index("ix_sys_audit_logs_created_at", "created_at"),
        {"comment": "Append-only audit trail of state-changing actions."},
    )

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        default=None,
        sort_order=ORDER_TENANT,
        comment="Tenant the event belongs to; NULL for platform-level events. No FK by design.",
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        default=None, comment="User who performed the action; NULL for system actions."
    )
    actor_type: Mapped[str] = mapped_column(
        String(20),
        default="user",
        server_default="user",
        comment='Kind of actor: "user" for JWT/RBAC-authenticated requests, '
        '"api_client" for API-key-authenticated partner requests.',
    )
    action: Mapped[str] = mapped_column(
        String(100), comment='Dotted action name, e.g. "user.update".'
    )
    resource_type: Mapped[str] = mapped_column(String(100), comment="Kind of resource acted on.")
    resource_id: Mapped[str | None] = mapped_column(
        String(100),
        default=None,
        comment="Id of the resource acted on (string: not always a UUID).",
    )
    before: Mapped[dict[str, Any] | None] = mapped_column(
        JsonB, default=None, comment="Snapshot of the resource before the change (PII-redacted)."
    )
    after: Mapped[dict[str, Any] | None] = mapped_column(
        JsonB, default=None, comment="Snapshot of the resource after the change (PII-redacted)."
    )
    request_id: Mapped[str | None] = mapped_column(
        String(64), default=None, comment="Correlates the event with request logs and traces."
    )
    ip_address: Mapped[str | None] = mapped_column(
        String(64),
        default=None,
        comment="Client address as seen by the API (text, may be a proxy value).",
    )
