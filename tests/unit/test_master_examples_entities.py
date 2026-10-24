from uuid import uuid4

from app.modules.master_examples.domain.entities import MasterExample


def test_audit_snapshot_contains_name_and_description() -> None:
    example = MasterExample(
        tenant_id=uuid4(), name="Default Currency", description="A reference row."
    )
    assert example.audit_snapshot() == {
        "name": "Default Currency",
        "description": "A reference row.",
    }


def test_default_description_is_empty_string() -> None:
    example = MasterExample(tenant_id=uuid4(), name="Default Currency")
    assert example.description == ""
