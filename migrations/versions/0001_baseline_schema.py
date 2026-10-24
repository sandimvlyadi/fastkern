"""baseline schema

Squashed initial schema, applying the project's database standards: `sys_` table
prefix, fixed column order (id, tenant_id, parent ids, main columns,
actors, timestamps), table/column comments, JSONB, server defaults, partial
unique indexes on soft-delete tables, and a trigger that maintains
`updated_at`. It replaces the three pre-release revisions (baseline,
tenant_id foreign keys, partial unique indexes) that had never been applied
to any database; Postgres cannot reorder columns in place, so a rename +
reorder chain would have been a table rebuild anyway.

Revision ID: 0001
Revises:
Create Date: 2026-09-26 17:33:33.317541

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.shared.infrastructure.pg_triggers import DROP_FUNCTION, updated_at_trigger_ddl

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sys_audit_logs",
        sa.Column("id", sa.Uuid(), nullable=False, comment="Primary key (UUIDv7, time-ordered)."),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            nullable=True,
            comment="Tenant the event belongs to; NULL for platform-level events. No FK by design.",
        ),
        sa.Column(
            "actor_id",
            sa.Uuid(),
            nullable=True,
            comment="User who performed the action; NULL for system actions.",
        ),
        sa.Column(
            "action",
            sa.String(length=100),
            nullable=False,
            comment='Dotted action name, e.g. "user.update".',
        ),
        sa.Column(
            "resource_type",
            sa.String(length=100),
            nullable=False,
            comment="Kind of resource acted on.",
        ),
        sa.Column(
            "resource_id",
            sa.String(length=100),
            nullable=True,
            comment="Id of the resource acted on (string: not always a UUID).",
        ),
        sa.Column(
            "before",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
            comment="Snapshot of the resource before the change (PII-redacted).",
        ),
        sa.Column(
            "after",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
            comment="Snapshot of the resource after the change (PII-redacted).",
        ),
        sa.Column(
            "request_id",
            sa.String(length=64),
            nullable=True,
            comment="Correlates the event with request logs and traces.",
        ),
        sa.Column(
            "ip_address",
            sa.String(length=64),
            nullable=True,
            comment="Client address as seen by the API (text, may be a proxy value).",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Row creation time (UTC).",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sys_audit_logs")),
        comment="Append-only audit trail of state-changing actions.",
    )
    op.create_index("ix_sys_audit_logs_created_at", "sys_audit_logs", ["created_at"], unique=False)
    op.create_index(
        "ix_sys_audit_logs_tenant_id_actor_id",
        "sys_audit_logs",
        ["tenant_id", "actor_id"],
        unique=False,
    )
    op.create_index(
        "ix_sys_audit_logs_tenant_id_created_at",
        "sys_audit_logs",
        ["tenant_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_sys_audit_logs_tenant_id_resource_type_resource_id",
        "sys_audit_logs",
        ["tenant_id", "resource_type", "resource_id"],
        unique=False,
    )
    op.create_table(
        "sys_tenants",
        sa.Column("id", sa.Uuid(), nullable=False, comment="Primary key (UUIDv7, time-ordered)."),
        sa.Column(
            "name", sa.String(length=255), nullable=False, comment="Display name of the tenant."
        ),
        sa.Column(
            "slug",
            sa.String(length=100),
            nullable=False,
            comment="URL-safe unique identifier; unique among live tenants.",
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
            comment="False suspends the tenant.",
        ),
        sa.Column("created_by", sa.Uuid(), nullable=True, comment="User id that created the row."),
        sa.Column(
            "updated_by", sa.Uuid(), nullable=True, comment="User id that last modified the row."
        ),
        sa.Column(
            "deleted_by", sa.Uuid(), nullable=True, comment="User id that soft-deleted the row."
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Row creation time (UTC).",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Last modification time (UTC); maintained by trigger.",
        ),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Soft-delete time (UTC); NULL while the row is live.",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sys_tenants")),
        comment="Tenants (organisations) that own all tenant-scoped data.",
    )
    op.create_index(
        "uq_sys_tenants_slug",
        "sys_tenants",
        ["slug"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_table(
        "sys_roles",
        sa.Column("id", sa.Uuid(), nullable=False, comment="Primary key (UUIDv7, time-ordered)."),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            nullable=False,
            comment="Owning tenant (row-level multi-tenancy).",
        ),
        sa.Column(
            "name", sa.String(length=100), nullable=False, comment="Role name; unique per tenant."
        ),
        sa.Column(
            "description",
            sa.String(length=500),
            server_default="",
            nullable=False,
            comment="Free-text description.",
        ),
        sa.Column(
            "permissions",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            server_default=sa.text("'[]'"),
            nullable=False,
            comment='Granted permissions as "resource:action" strings.',
        ),
        sa.Column(
            "is_system",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
            comment="Built-in role managed by the platform; not editable.",
        ),
        sa.Column("created_by", sa.Uuid(), nullable=True, comment="User id that created the row."),
        sa.Column(
            "updated_by", sa.Uuid(), nullable=True, comment="User id that last modified the row."
        ),
        sa.Column(
            "deleted_by", sa.Uuid(), nullable=True, comment="User id that soft-deleted the row."
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Row creation time (UTC).",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Last modification time (UTC); maintained by trigger.",
        ),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Soft-delete time (UTC); NULL while the row is live.",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["sys_tenants.id"], name=op.f("fk_sys_roles_tenant_id_sys_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sys_roles")),
        comment="RBAC roles and the permissions each one grants, per tenant.",
    )
    op.create_index(
        "uq_sys_roles_tenant_name",
        "sys_roles",
        ["tenant_id", "name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_table(
        "sys_users",
        sa.Column("id", sa.Uuid(), nullable=False, comment="Primary key (UUIDv7, time-ordered)."),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            nullable=False,
            comment="Owning tenant (row-level multi-tenancy).",
        ),
        sa.Column(
            "email",
            sa.String(length=320),
            nullable=False,
            comment="Login email; unique per tenant.",
        ),
        sa.Column(
            "hashed_password",
            sa.String(length=255),
            nullable=False,
            comment="Password hash (never plain).",
        ),
        sa.Column("full_name", sa.String(length=255), nullable=False, comment="Display name."),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
            comment="False blocks login.",
        ),
        sa.Column(
            "locale",
            sa.String(length=10),
            server_default="en",
            nullable=False,
            comment="Preferred language (BCP 47).",
        ),
        sa.Column(
            "timezone",
            sa.String(length=64),
            server_default="UTC",
            nullable=False,
            comment="Preferred IANA timezone.",
        ),
        sa.Column("created_by", sa.Uuid(), nullable=True, comment="User id that created the row."),
        sa.Column(
            "updated_by", sa.Uuid(), nullable=True, comment="User id that last modified the row."
        ),
        sa.Column(
            "deleted_by", sa.Uuid(), nullable=True, comment="User id that soft-deleted the row."
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Row creation time (UTC).",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Last modification time (UTC); maintained by trigger.",
        ),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Soft-delete time (UTC); NULL while the row is live.",
        ),
        sa.CheckConstraint("email = lower(email)", name=op.f("ck_sys_users_email_lower")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["sys_tenants.id"], name=op.f("fk_sys_users_tenant_id_sys_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sys_users")),
        comment="User accounts (authentication identity), scoped to a tenant.",
    )
    op.create_index(
        "uq_sys_users_tenant_email",
        "sys_users",
        ["tenant_id", "email"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_table(
        "sys_user_roles",
        sa.Column("id", sa.Uuid(), nullable=False, comment="Primary key (UUIDv7, time-ordered)."),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            nullable=False,
            comment="Owning tenant (row-level multi-tenancy).",
        ),
        sa.Column("user_id", sa.Uuid(), nullable=False, comment="The user holding the role."),
        sa.Column("role_id", sa.Uuid(), nullable=False, comment="The role granted."),
        sa.Column(
            "is_default",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
            comment="Whether this is the user's default role; used as `active_role` at login.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Row creation time (UTC).",
        ),
        sa.ForeignKeyConstraint(
            ["role_id"],
            ["sys_roles.id"],
            name=op.f("fk_sys_user_roles_role_id_sys_roles"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["sys_tenants.id"], name=op.f("fk_sys_user_roles_tenant_id_sys_tenants")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["sys_users.id"],
            name=op.f("fk_sys_user_roles_user_id_sys_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sys_user_roles")),
        sa.UniqueConstraint(
            "tenant_id", "user_id", "role_id", name="uq_sys_user_roles_tenant_user_role"
        ),
        comment="Grants: which roles a user holds within a tenant (many-to-many).",
    )
    op.create_index(op.f("ix_sys_user_roles_role_id"), "sys_user_roles", ["role_id"], unique=False)
    op.create_index(op.f("ix_sys_user_roles_user_id"), "sys_user_roles", ["user_id"], unique=False)
    op.create_index(
        "uq_sys_user_roles_tenant_user_default",
        "sys_user_roles",
        ["tenant_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )

    # Not detected by autogenerate: keep updated_at honest for bulk UPDATEs, psql, ETL.
    for statement in updated_at_trigger_ddl(("sys_tenants", "sys_roles", "sys_users")):
        op.execute(statement)


def downgrade() -> None:
    op.drop_index(
        "uq_sys_user_roles_tenant_user_default",
        table_name="sys_user_roles",
        postgresql_where=sa.text("is_default"),
    )
    op.drop_index(op.f("ix_sys_user_roles_user_id"), table_name="sys_user_roles")
    op.drop_index(op.f("ix_sys_user_roles_role_id"), table_name="sys_user_roles")
    op.drop_table("sys_user_roles")
    op.drop_index(
        "uq_sys_users_tenant_email",
        table_name="sys_users",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.drop_table("sys_users")
    op.drop_index(
        "uq_sys_roles_tenant_name",
        table_name="sys_roles",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.drop_table("sys_roles")
    op.drop_index(
        "uq_sys_tenants_slug",
        table_name="sys_tenants",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.drop_table("sys_tenants")
    op.drop_index(
        "ix_sys_audit_logs_tenant_id_resource_type_resource_id", table_name="sys_audit_logs"
    )
    op.drop_index("ix_sys_audit_logs_tenant_id_created_at", table_name="sys_audit_logs")
    op.drop_index("ix_sys_audit_logs_tenant_id_actor_id", table_name="sys_audit_logs")
    op.drop_index("ix_sys_audit_logs_created_at", table_name="sys_audit_logs")
    op.drop_table("sys_audit_logs")
    op.execute(DROP_FUNCTION)  # the triggers went with their tables
