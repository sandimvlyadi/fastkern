import dataclasses
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.modules.audit_log.domain.entities import AuditLogEntry
from app.modules.roles.domain.entities import Role
from app.modules.tenants.domain.entities import Tenant
from app.modules.users.domain.entities import User
from app.shared.domain.entity import AuditedEntity, Entity


def test_user_deactivate_activate() -> None:
    user = User(tenant_id=uuid4(), email="a@example.com", hashed_password="x", full_name="A")
    assert user.is_active is True
    user.deactivate()
    assert user.is_active is False
    user.activate()
    assert user.is_active is True


def test_role_has_permission() -> None:
    role = Role(tenant_id=uuid4(), name="editor", permissions=["users:read"])
    assert role.has_permission("users:read")
    assert not role.has_permission("users:delete")


def test_role_wildcard_permission() -> None:
    role = Role(tenant_id=uuid4(), name="admin", permissions=["*"])
    assert role.has_permission("anything:whatsoever")


def test_user_change_password() -> None:
    user = User(tenant_id=uuid4(), email="a@example.com", hashed_password="old", full_name="A")
    user.change_password("new-hash")
    assert user.hashed_password == "new-hash"


def test_entity_equality_same_type_and_id() -> None:
    entity_id = uuid4()
    a = Entity(id=entity_id)
    b = Entity(id=entity_id)
    assert a == b
    assert hash(a) == hash(b)


def test_entity_equality_different_type_same_id_not_equal() -> None:
    entity_id = uuid4()

    @dataclasses.dataclass(kw_only=True)
    class OtherEntity(Entity):
        pass

    assert Entity(id=entity_id) != OtherEntity(id=entity_id)


def test_entity_equality_with_non_entity_returns_not_implemented() -> None:
    assert Entity() != "not-an-entity"
    assert Entity().__eq__("not-an-entity") is NotImplemented


def test_audited_entity_is_deleted() -> None:
    entity = AuditedEntity()
    assert entity.is_deleted is False
    entity.deleted_at = datetime.now(UTC)
    assert entity.is_deleted is True


def test_tenant_defaults() -> None:
    tenant = Tenant(name="Acme", slug="acme")
    assert tenant.is_active is True


def test_audit_log_entry_is_frozen() -> None:
    entry = AuditLogEntry(
        tenant_id=uuid4(),
        actor_id=uuid4(),
        action="user.created",
        resource_type="user",
        resource_id="1",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.action = "user.updated"  # type: ignore[misc]


def test_audit_log_entry_defaults() -> None:
    entry = AuditLogEntry(
        tenant_id=None,
        actor_id=None,
        action="tenant.created",
        resource_type="tenant",
        resource_id=None,
    )
    assert entry.before is None
    assert entry.after is None
    assert entry.request_id is None
    assert entry.ip_address is None
    assert entry.created_at is None


def test_user_audit_snapshot_never_includes_password_hash() -> None:
    user = User(
        tenant_id=uuid4(), email="a@example.com", hashed_password="secret-hash", full_name="A"
    )

    snapshot = user.audit_snapshot()

    assert snapshot == {
        "email": "a@example.com",
        "fullName": "A",
        "isActive": True,
        "locale": "en",
        "timezone": "UTC",
    }
    assert "secret-hash" not in str(snapshot)


def test_role_audit_snapshot_copies_permissions() -> None:
    role = Role(tenant_id=uuid4(), name="editor", permissions=["users:read"])

    snapshot = role.audit_snapshot()
    role.permissions.append("users:write")

    assert snapshot == {
        "name": "editor",
        "description": "",
        "permissions": ["users:read"],
        "isSystem": False,
    }


def test_tenant_audit_snapshot() -> None:
    assert Tenant(name="Acme", slug="acme").audit_snapshot() == {
        "name": "Acme",
        "slug": "acme",
        "isActive": True,
    }
