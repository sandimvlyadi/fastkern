"""add trans_examples tables

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-02 14:58:31.655192

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.shared.infrastructure.pg_triggers import updated_at_trigger_ddl

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trans_examples",
        sa.Column("id", sa.Uuid(), nullable=False, comment="Primary key (UUIDv7, time-ordered)."),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            nullable=False,
            comment="Owning tenant (row-level multi-tenancy).",
        ),
        sa.Column(
            "master_example_id",
            sa.Uuid(),
            nullable=True,
            comment="Optional reference to a master example (many-to-one; not unique).",
        ),
        sa.Column("name", sa.String(length=255), nullable=False, comment="Display name."),
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
            ["master_example_id"],
            ["master_examples.id"],
            name=op.f("fk_trans_examples_master_example_id_master_examples"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["sys_tenants.id"], name=op.f("fk_trans_examples_tenant_id_sys_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trans_examples")),
        comment="Example business-transaction row, optionally referencing a master example.",
    )
    op.create_index(
        op.f("ix_trans_examples_master_example_id"),
        "trans_examples",
        ["master_example_id"],
        unique=False,
    )
    op.create_index(
        "ix_trans_examples_tenant_created",
        "trans_examples",
        ["tenant_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "trans_example_items",
        sa.Column("id", sa.Uuid(), nullable=False, comment="Primary key (UUIDv7, time-ordered)."),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            nullable=False,
            comment="Owning tenant (row-level multi-tenancy).",
        ),
        sa.Column(
            "example_id",
            sa.Uuid(),
            nullable=False,
            comment="The transaction example this item belongs to.",
        ),
        sa.Column("label", sa.String(length=255), nullable=False, comment="Item label."),
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
            ["example_id"],
            ["trans_examples.id"],
            name=op.f("fk_trans_example_items_example_id_trans_examples"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["sys_tenants.id"],
            name=op.f("fk_trans_example_items_tenant_id_sys_tenants"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trans_example_items")),
        comment="One-to-many child row of a transaction example (e.g. a tag/label).",
    )
    op.create_index(
        op.f("ix_trans_example_items_example_id"),
        "trans_example_items",
        ["example_id"],
        unique=False,
    )
    op.create_index(
        "ix_trans_example_items_tenant_example",
        "trans_example_items",
        ["tenant_id", "example_id"],
        unique=False,
    )
    for statement in updated_at_trigger_ddl(("trans_examples", "trans_example_items")):
        op.execute(statement)


def downgrade() -> None:
    op.drop_index("ix_trans_example_items_tenant_example", table_name="trans_example_items")
    op.drop_index(op.f("ix_trans_example_items_example_id"), table_name="trans_example_items")
    op.drop_table("trans_example_items")
    op.drop_index("ix_trans_examples_tenant_created", table_name="trans_examples")
    op.drop_index(op.f("ix_trans_examples_master_example_id"), table_name="trans_examples")
    op.drop_table("trans_examples")
