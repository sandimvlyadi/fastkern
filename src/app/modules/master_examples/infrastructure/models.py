from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.infrastructure.orm_base import StandardModel, soft_delete_unique_index


class MasterExampleModel(StandardModel):
    __tablename__ = "master_examples"
    __table_args__ = (
        # Partial: a soft-deleted master example must not keep its name reserved, or
        # re-creating it would pass the use case's duplicate check (which ignores deleted
        # rows) and then fail here with an IntegrityError.
        soft_delete_unique_index("uq_master_examples_tenant_name", "tenant_id", "name"),
        {"comment": "Example reference/master data row, scoped per tenant."},
    )

    name: Mapped[str] = mapped_column(String(255), comment="Display name; unique per tenant.")
    description: Mapped[str] = mapped_column(
        String(1000), default="", server_default="", comment="Free-text description."
    )
