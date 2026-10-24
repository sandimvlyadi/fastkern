from uuid import uuid4

from app.modules.examples.domain.entities import Example, ExampleItem


def test_audit_snapshot_contains_all_fields() -> None:
    master_id = uuid4()
    example = Example(
        tenant_id=uuid4(),
        name="Invoice #1",
        description="desc",
        master_example_id=master_id,
        items=[ExampleItem(label="b"), ExampleItem(label="a")],
    )
    snapshot = example.audit_snapshot()
    assert snapshot["name"] == "Invoice #1"
    assert snapshot["description"] == "desc"
    assert snapshot["masterExampleId"] == str(master_id)
    assert snapshot["items"] == ["a", "b"]


def test_audit_snapshot_sorts_items_regardless_of_insertion_order() -> None:
    example = Example(
        tenant_id=uuid4(), name="X", items=[ExampleItem(label="z"), ExampleItem(label="a")]
    )
    assert example.audit_snapshot()["items"] == ["a", "z"]


def test_null_master_example_id_snapshots_as_none() -> None:
    example = Example(tenant_id=uuid4(), name="X")
    assert example.audit_snapshot()["masterExampleId"] is None
    assert example.master_example_id is None
    assert example.items == []
