import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.infrastructure.orm_base import (
    ORDER_PARENT,
    Base,
    CreatedAtMixin,
    JsonB,
    StandardModel,
    TenantMixin,
    UUIDPrimaryKeyMixin,
    soft_delete_unique_index,
)


class ApiKeyModel(StandardModel):
    __tablename__ = "sys_api_keys"
    __table_args__ = (
        soft_delete_unique_index("uq_sys_api_keys_tenant_key_prefix", "tenant_id", "key_prefix"),
        # Global, not tenant-scoped: verifying an inbound API key happens before the caller's
        # tenant is known at all (same bootstrap problem `sys_users` email lookup solves at
        # login via `tenant_scoped=False`). Covers the rotation overlap window too.
        Index("ix_sys_api_keys_key_hash", "key_hash"),
        Index("ix_sys_api_keys_previous_key_hash", "previous_key_hash"),
        {"comment": "Partner (B2B) API keys: inbound-auth credentials, distinct from sys_users."},
    )

    name: Mapped[str] = mapped_column(String(200), comment="Human-readable label for this key.")
    key_prefix: Mapped[str] = mapped_column(
        String(16), comment="Non-secret prefix of the current plaintext key, for admin display."
    )
    key_hash: Mapped[str] = mapped_column(
        String(128), comment="SHA-256 hex digest of the current plaintext key."
    )
    previous_key_hash: Mapped[str | None] = mapped_column(
        String(128),
        default=None,
        comment="Prior key's hash, kept valid during a rotation overlap window.",
    )
    previous_key_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        default=None,
        comment="When the rotation overlap window for previous_key_hash ends (UTC).",
    )
    scopes: Mapped[list[str]] = mapped_column(
        JsonB,
        default=list,
        server_default=text("'[]'"),
        comment='Granted scopes as "resource:action" strings; independent of RBAC permissions.',
    )
    status: Mapped[str] = mapped_column(
        String(20),
        default="active",
        server_default=text("'active'"),
        comment='Lifecycle state: "active" or "revoked".',
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None, comment="Optional hard expiry (UTC)."
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None, comment="Last time this key authenticated a request."
    )


class ApiRequestLogModel(UUIDPrimaryKeyMixin, CreatedAtMixin, TenantMixin, Base):
    """Append-only raw traffic log for API-key-authenticated requests. Deliberately separate
    from `sys_audit_logs`: this is observability/billing-support data with its own
    short retention (30-90 days, purged by a Celery beat job), not a domain-event audit trail."""

    __tablename__ = "sys_api_request_logs"
    __table_args__ = (
        Index("ix_sys_api_request_logs_tenant_id_created_at", "tenant_id", "created_at"),
        Index("ix_sys_api_request_logs_api_key_id_created_at", "api_key_id", "created_at"),
        Index("ix_sys_api_request_logs_created_at", "created_at"),
        {"comment": "Append-only raw traffic log for partner API-key-authenticated requests."},
    )

    api_key_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sys_api_keys.id", ondelete="CASCADE"),
        sort_order=ORDER_PARENT,
        comment="The API key that authenticated this request.",
    )
    method: Mapped[str] = mapped_column(String(10), comment="HTTP method.")
    path: Mapped[str] = mapped_column(String(500), comment="Request path.")
    status_code: Mapped[int] = mapped_column(Integer, comment="Response status code.")
    latency_ms: Mapped[int] = mapped_column(Integer, comment="Request latency in milliseconds.")
    response_size_bytes: Mapped[int | None] = mapped_column(
        Integer, default=None, comment="Response body size in bytes, when known."
    )
