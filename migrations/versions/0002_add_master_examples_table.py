"""add master_examples table

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-02 14:46:56.060578

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.shared.infrastructure.pg_triggers import updated_at_trigger_ddl

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "master_examples",
        sa.Column("id", sa.Uuid(), nullable=False, comment="Primary key (UUIDv7, time-ordered)."),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            nullable=False,
            comment="Owning tenant (row-level multi-tenancy).",
        ),
        sa.Column(
            "name",
            sa.String(length=255),
            nullable=False,
            comment="Display name; unique per tenant.",
        ),
        sa.Column(
            "description",
            sa.String(length=1000),
            server_default="",
            nullable=False,
            comment="Free-text description.",
        ),
        sa.Column("created_by", sa.Uuid(), nullable=True, comment="User id that created the row."),
        sa.Column(
            "updated_by",
            sa.Uuid(),
            nullable=True,
            comment="User id that last modified the row.",
        ),
        sa.Column(
            "deleted_by",
            sa.Uuid(),
            nullable=True,
            comment="User id that soft-deleted the row.",
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
            ["tenant_id"], ["sys_tenants.id"], name=op.f("fk_master_examples_tenant_id_sys_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_master_examples")),
        comment="Example reference/master data row, scoped per tenant.",
    )
    op.create_index(
        "uq_master_examples_tenant_name",
        "master_examples",
        ["tenant_id", "name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
        sqlite_where=sa.text("deleted_at IS NULL"),
    )
    for statement in updated_at_trigger_ddl(("master_examples",)):
        op.execute(statement)


def downgrade() -> None:
    op.drop_index(
        "uq_master_examples_tenant_name",
        table_name="master_examples",
        postgresql_where=sa.text("deleted_at IS NULL"),
        sqlite_where=sa.text("deleted_at IS NULL"),
    )
    op.drop_table("master_examples")
