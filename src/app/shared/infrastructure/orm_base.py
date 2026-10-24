"""SQLAlchemy declarative base + reusable column mixins.

Every module's ORM model composes these instead of redeclaring
created_at/updated_at/tenant_id/soft-delete columns by hand, so the
"enterprise standard columns" are structurally guaranteed, not a
convention people can forget.

Database standards enforced here and by `tests/unit/test_schema_standards.py`:

* Table names carry a purpose prefix — `sys_` (platform/identity/governance),
  `master_` (reference data), `trans_` (business transactions).
* Column order is fixed. Postgres cannot reorder columns after the fact, so
  it is set at model level with `mapped_column(sort_order=...)`:
  id -> tenant_id -> parent/FK ids -> main columns -> actors
  (created_by, updated_by, deleted_by) -> timestamps (created_at,
  updated_at, deleted_at). The `ORDER_*` constants below are the buckets;
  a model's own columns default to 0, i.e. the "main columns" bucket.
* Every table and column has a `comment` (the data dictionary).
* A NOT NULL column with a Python-side default also has a `server_default`,
  so raw SQL / ETL inserts behave like ORM inserts.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, MetaData, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.shared.domain.identifiers import uuid7
from app.shared.infrastructure import pg_triggers

# Explicit naming convention so Alembic autogenerate produces stable,
# predictable constraint/index names across environments.
_NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# Column-order buckets (see module docstring). Lower sorts first; columns
# with no explicit sort_order are 0 = the "main columns" bucket.
ORDER_ID = -100
ORDER_TENANT = -90
ORDER_PARENT = -80  # parent / foreign-key id columns of a child table
ORDER_CREATED_BY = 100
ORDER_UPDATED_BY = 101
ORDER_DELETED_BY = 102
ORDER_CREATED_AT = 200
ORDER_UPDATED_AT = 201
ORDER_DELETED_AT = 202

# JSONB on Postgres (indexable, queryable, binary-stored); plain JSON on
# SQLite so the fast local test database keeps working.
JsonB = JSON().with_variant(JSONB(), "postgresql")

_LIVE_ROWS = text("deleted_at IS NULL")


def soft_delete_unique_index(name: str, *columns: str) -> Index:
    """Unique index that only covers live rows.

    On a soft-delete table a plain UNIQUE keeps a deleted row's value
    reserved forever: the use case's duplicate check (which ignores deleted
    rows) passes and the INSERT then fails with an IntegrityError. Every
    unique key on a table with `deleted_at` must be built with this.
    """
    return Index(name, *columns, unique=True, postgresql_where=_LIVE_ROWS, sqlite_where=_LIVE_ROWS)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=_NAMING_CONVENTION)


class UUIDPrimaryKeyMixin:
    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid7,
        sort_order=ORDER_ID,
        comment="Primary key (UUIDv7, time-ordered).",
    )


class CreatedAtMixin:
    # DateTime(timezone=True) makes "everything is UTC" a structural
    # guarantee (TIMESTAMPTZ on Postgres) rather than convention only.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        sort_order=ORDER_CREATED_AT,
        comment="Row creation time (UTC).",
    )


class TimestampMixin(CreatedAtMixin):
    # `onupdate` covers UPDATEs through the ORM; the Postgres trigger in
    # `pg_triggers.py` covers everything else (bulk UPDATE, psql, ETL).
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        sort_order=ORDER_UPDATED_AT,
        comment="Last modification time (UTC); maintained by trigger.",
    )


class AuditMixin:
    """Who created/last-modified the row, in addition to when (TimestampMixin).

    Plain UUIDs with no FK on purpose: an FK to `sys_users` would be
    circular for `sys_users` itself and for `sys_tenants`, and would block
    hard-deleting a user (GDPR erasure) that once touched other rows.

    Populated by `BaseRepository` (add/save/delete) from the request context.
    """

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        default=None, sort_order=ORDER_CREATED_BY, comment="User id that created the row."
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        default=None, sort_order=ORDER_UPDATED_BY, comment="User id that last modified the row."
    )


class SoftDeleteMixin:
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(
        default=None, sort_order=ORDER_DELETED_BY, comment="User id that soft-deleted the row."
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        default=None,
        sort_order=ORDER_DELETED_AT,
        comment="Soft-delete time (UTC); NULL while the row is live.",
    )

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def soft_delete(self, actor_id: uuid.UUID | None = None) -> None:
        self.deleted_at = datetime.now(UTC)
        self.deleted_by = actor_id


class TenantMixin:
    """Row-level multi-tenancy: every tenant-owned table carries tenant_id
    and every repository query MUST filter by it (see BaseRepository)."""

    # FK by table name (not by importing TenantModel) so `shared` stays
    # independent of the tenants module; it makes "a row can only belong to a
    # real tenant" a database guarantee, not just a use-case check.
    #
    # Deliberately NOT indexed on its own: every tenant-owned table needs a
    # composite index/unique led by tenant_id that matches its real access
    # path (e.g. (tenant_id, email)), which already serves tenant_id lookups;
    # a bare index would be pure write overhead. Enforced by
    # tests/unit/test_schema_standards.py.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sys_tenants.id"),
        sort_order=ORDER_TENANT,
        comment="Owning tenant (row-level multi-tenancy).",
    )


class StandardModel(
    UUIDPrimaryKeyMixin, TimestampMixin, AuditMixin, SoftDeleteMixin, TenantMixin, Base
):
    """Convenience base combining every cross-cutting column. Use this for
    tenant-owned aggregates."""

    __abstract__ = True


class GlobalModel(UUIDPrimaryKeyMixin, TimestampMixin, AuditMixin, SoftDeleteMixin, Base):
    """Same as StandardModel but WITHOUT tenant_id — for system-wide tables
    that are not owned by a tenant (e.g. `sys_tenants` itself, global role
    catalog)."""

    __abstract__ = True


pg_triggers.install(Base.metadata)
