from sqlalchemy import DateTime

from app.modules.audit_log.infrastructure.models import AuditLogModel
from app.modules.users.infrastructure.models import UserModel
from app.shared.domain.identifiers import uuid7


def test_soft_delete_mixin_is_deleted_property() -> None:
    model = UserModel(email="a@example.com", hashed_password="h", full_name="A")
    assert model.is_deleted is False
    model.soft_delete()
    assert model.is_deleted is True
    assert model.deleted_by is None


def test_soft_delete_records_the_acting_user() -> None:
    actor_id = uuid7()
    model = UserModel(email="a@example.com", hashed_password="h", full_name="A")
    model.soft_delete(actor_id)
    assert model.deleted_by == actor_id
    assert model.deleted_at is not None


def test_standard_model_timestamp_columns_are_timezone_aware() -> None:
    """UTC-ness must be structurally enforced (Postgres TIMESTAMPTZ), not
    convention only.
    """
    columns = UserModel.__table__.columns
    for name in ("created_at", "updated_at", "deleted_at"):
        column_type = columns[name].type
        assert isinstance(column_type, DateTime)
        assert column_type.timezone is True, f"{name} must be DateTime(timezone=True)"


def test_audit_log_timestamp_column_is_timezone_aware() -> None:
    """AuditLogModel is append-only (CreatedAtMixin only, no TimestampMixin) —
    it must not be forgotten when hardening UTC-ness."""
    column_type = AuditLogModel.__table__.columns["created_at"].type
    assert isinstance(column_type, DateTime)
    assert column_type.timezone is True


def test_tenant_owned_tables_reference_tenants_table() -> None:
    """A tenant-owned row can only belong to a real tenant — enforced by the
    database (FK), not only by the register use case."""
    from app.modules.roles.infrastructure.models import RoleModel, UserRoleModel

    for model in (UserModel, RoleModel, UserRoleModel):
        targets = {fk.target_fullname for fk in model.__table__.columns["tenant_id"].foreign_keys}
        assert targets == {"sys_tenants.id"}, model.__name__
