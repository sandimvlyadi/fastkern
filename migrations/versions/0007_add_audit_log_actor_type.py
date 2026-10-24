"""add audit log actor_type

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-12 01:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sys_audit_logs",
        sa.Column(
            "actor_type",
            sa.String(length=20),
            server_default="user",
            nullable=False,
            comment='Kind of actor: "user" or "api_client".',
        ),
    )


def downgrade() -> None:
    op.drop_column("sys_audit_logs", "actor_type")
