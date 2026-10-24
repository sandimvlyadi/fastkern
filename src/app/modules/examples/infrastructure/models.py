import uuid

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.shared.infrastructure.orm_base import ORDER_PARENT, StandardModel


class ExampleModel(StandardModel):
    __tablename__ = "trans_examples"
    __table_args__ = (
        # Real access pattern (list newest-first per tenant), and what makes tenant_id
        # "lead an index" for test_schema_standards.py's check_index_coverage.
        Index("ix_trans_examples_tenant_created", "tenant_id", "created_at"),
        {"comment": "Example business-transaction row, optionally referencing a master example."},
    )

    master_example_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("master_examples.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
        sort_order=ORDER_PARENT,
        comment="Optional reference to a master example (many-to-one; not unique).",
    )
    name: Mapped[str] = mapped_column(String(255), comment="Display name.")
    description: Mapped[str] = mapped_column(
        String(1000), default="", server_default="", comment="Free-text description."
    )
    items: Mapped[list[ExampleItemModel]] = relationship(
        back_populates="example",
        cascade="all, delete-orphan",
        order_by="ExampleItemModel.created_at",
    )


class ExampleItemModel(StandardModel):
    __tablename__ = "trans_example_items"
    __table_args__ = (
        # tenant_id must lead some index (check_index_coverage); example_id already has its
        # own index=True below, so this composite exists purely to satisfy that rule with a
        # real access path ("this tenant's items for a given example").
        Index("ix_trans_example_items_tenant_example", "tenant_id", "example_id"),
        {"comment": "One-to-many child row of a transaction example (e.g. a tag/label)."},
    )

    example_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trans_examples.id", ondelete="CASCADE"),
        index=True,
        sort_order=ORDER_PARENT,
        comment="The transaction example this item belongs to.",
    )
    label: Mapped[str] = mapped_column(String(255), comment="Item label.")
    example: Mapped[ExampleModel] = relationship(back_populates="items")
