import uuid

from sqlalchemy import CheckConstraint, ForeignKey, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.infrastructure.orm_base import ORDER_PARENT, StandardModel, soft_delete_unique_index


class UserModel(StandardModel):
    __tablename__ = "sys_users"
    __table_args__ = (
        # Partial: a soft-deleted user must not keep its email reserved.
        # Led by tenant_id, so it also serves every tenant-scoped user lookup.
        soft_delete_unique_index("uq_sys_users_tenant_email", "tenant_id", "email"),
        # The unique index above is case-sensitive, so without this `Jane@x.io` and `jane@x.io`
        # would be two accounts. The app lowercases on the way in; this makes it structural.
        # Named "email_lower" -> ck_sys_users_email_lower (see the ORM naming convention).
        CheckConstraint("email = lower(email)", name="email_lower"),
        {"comment": "User accounts (authentication identity), scoped to a tenant."},
    )

    email: Mapped[str] = mapped_column(String(320), comment="Login email; unique per tenant.")
    hashed_password: Mapped[str] = mapped_column(
        String(255), comment="Password hash (never plain)."
    )
    full_name: Mapped[str] = mapped_column(String(255), comment="Display name.")
    is_active: Mapped[bool] = mapped_column(
        default=True, server_default=text("true"), comment="False blocks login."
    )
    locale: Mapped[str] = mapped_column(
        String(10), default="en", server_default="en", comment="Preferred language (BCP 47)."
    )
    timezone: Mapped[str] = mapped_column(
        String(64), default="UTC", server_default="UTC", comment="Preferred IANA timezone."
    )
    avatar_file_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("trans_files.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        sort_order=ORDER_PARENT,
        comment="The user's current profile picture (files module FileAsset), if any.",
    )
