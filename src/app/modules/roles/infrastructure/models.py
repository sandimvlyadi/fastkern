import uuid

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.infrastructure.orm_base import (
    ORDER_PARENT,
    Base,
    CreatedAtMixin,
    JsonB,
    StandardModel,
    TenantMixin,
    UUIDPrimaryKeyMixin,
    soft_delete_unique_index,
)


class RoleModel(StandardModel):
    __tablename__ = "sys_roles"
    __table_args__ = (
        # Partial: a soft-deleted role must not keep its name reserved, or
        # re-creating it would pass the use case's duplicate check (which
        # ignores deleted rows) and then fail here with an IntegrityError.
        soft_delete_unique_index("uq_sys_roles_tenant_name", "tenant_id", "name"),
        {"comment": "RBAC roles and the permissions each one grants, per tenant."},
    )

    name: Mapped[str] = mapped_column(String(100), comment="Role name; unique per tenant.")
    description: Mapped[str] = mapped_column(
        String(500), default="", server_default="", comment="Free-text description."
    )
    permissions: Mapped[list[str]] = mapped_column(
        JsonB,
        default=list,
        server_default=text("'[]'"),
        comment='Granted permissions as "resource:action" strings.',
    )
    is_system: Mapped[bool] = mapped_column(
        default=False,
        server_default=text("false"),
        comment="Built-in role managed by the platform; not editable.",
    )


class UserRoleModel(UUIDPrimaryKeyMixin, CreatedAtMixin, TenantMixin, Base):
    """Association table: which roles a user holds within a tenant.
    A user can hold several roles and switch which one is "active" per
    session via the JWT `active_role` claim (see auth module).

    A pure junction: a grant is inserted and revoked (hard delete), never
    edited or soft-deleted, so it has no updated_at / actor / deleted_*
    columns — the audit log records who granted or revoked.
    """

    __tablename__ = "sys_user_roles"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "user_id", "role_id", name="uq_sys_user_roles_tenant_user_role"
        ),
        # Partial: the DB-level guarantee that a user has at most one default
        # role, backing SetDefaultRoleUseCase's check-then-update (see
        # UserRoleRepository.set_default_role).
        Index(
            "uq_sys_user_roles_tenant_user_default",
            "tenant_id",
            "user_id",
            unique=True,
            postgresql_where=text("is_default"),
            sqlite_where=text("is_default"),
        ),
        {"comment": "Grants: which roles a user holds within a tenant (many-to-many)."},
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sys_users.id", ondelete="CASCADE"),
        index=True,
        sort_order=ORDER_PARENT,
        comment="The user holding the role.",
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sys_roles.id", ondelete="CASCADE"),
        index=True,
        sort_order=ORDER_PARENT + 1,
        comment="The role granted.",
    )
    is_default: Mapped[bool] = mapped_column(
        default=False,
        server_default=text("false"),
        comment="Whether this is the user's default role; used as `active_role` at login.",
    )
