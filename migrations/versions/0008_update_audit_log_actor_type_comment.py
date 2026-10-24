"""update audit log actor_type column comment

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-12 03:13:34.512979

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "sys_audit_logs",
        "actor_type",
        existing_type=sa.VARCHAR(length=20),
        comment='Kind of actor: "user" for JWT/RBAC-authenticated requests, '
        '"api_client" for API-key-authenticated partner requests.',
        existing_comment='Kind of actor: "user" or "api_client".',
        existing_nullable=False,
        existing_server_default=sa.text("'user'::character varying"),
    )


def downgrade() -> None:
    op.alter_column(
        "sys_audit_logs",
        "actor_type",
        existing_type=sa.VARCHAR(length=20),
        comment='Kind of actor: "user" or "api_client".',
        existing_comment='Kind of actor: "user" for JWT/RBAC-authenticated requests, '
        '"api_client" for API-key-authenticated partner requests.',
        existing_nullable=False,
        existing_server_default=sa.text("'user'::character varying"),
    )
