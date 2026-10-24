from sqlalchemy import String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.infrastructure.orm_base import GlobalModel, soft_delete_unique_index


class TenantModel(GlobalModel):
    __tablename__ = "sys_tenants"
    __table_args__ = (
        # Partial unique index (also serves slug lookups, which always
        # exclude soft-deleted rows): a deleted tenant must not keep its slug
        # reserved — see soft_delete_unique_index().
        soft_delete_unique_index("uq_sys_tenants_slug", "slug"),
        {"comment": "Tenants (organisations) that own all tenant-scoped data."},
    )

    name: Mapped[str] = mapped_column(String(255), comment="Display name of the tenant.")
    slug: Mapped[str] = mapped_column(
        String(100), comment="URL-safe unique identifier; unique among live tenants."
    )
    is_active: Mapped[bool] = mapped_column(
        default=True, server_default=text("true"), comment="False suspends the tenant."
    )
