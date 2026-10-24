from uuid import uuid4

import pytest

from app.core.pagination import OffsetParams
from app.modules.examples.application.dto import CreateExampleInput, UpdateExampleInput
from app.modules.examples.application.use_cases import (
    CreateExampleUseCase,
    DeleteExampleUseCase,
    GetExampleUseCase,
    ListExamplesUseCase,
    UpdateExampleUseCase,
)
from app.modules.examples.domain.entities import Example
from app.modules.examples.domain.exceptions import ExampleNotFoundError
from app.modules.master_examples.application.dto import CreateMasterExampleInput
from app.modules.master_examples.application.use_cases import CreateMasterExampleUseCase
from app.modules.master_examples.domain.exceptions import MasterExampleNotFoundError
from tests.fakes import InMemoryExampleRepository, InMemoryMasterExampleRepository


async def test_create_with_unknown_master_example_id_raises() -> None:
    examples = InMemoryExampleRepository()
    masters = InMemoryMasterExampleRepository()
    with pytest.raises(MasterExampleNotFoundError):
        await CreateExampleUseCase(examples, masters).execute(
            CreateExampleInput(name="Invoice #1", master_example_id=uuid4()), uuid4()
        )


async def test_create_with_null_master_example_id_succeeds_without_lookup() -> None:
    examples = InMemoryExampleRepository()
    masters = InMemoryMasterExampleRepository()
    tenant_id = uuid4()
    example = await CreateExampleUseCase(examples, masters).execute(
        CreateExampleInput(name="Invoice #1"), tenant_id
    )
    assert example.master_example_id is None
    assert example.items == []


async def test_create_with_valid_master_example_id_succeeds() -> None:
    examples = InMemoryExampleRepository()
    masters = InMemoryMasterExampleRepository()
    tenant_id = uuid4()
    master = await CreateMasterExampleUseCase(masters).execute(
        CreateMasterExampleInput(name="Default Currency"), tenant_id
    )
    example = await CreateExampleUseCase(examples, masters).execute(
        CreateExampleInput(name="Invoice #1", master_example_id=master.id, items=["a", "b"]),
        tenant_id,
    )
    assert example.master_example_id == master.id
    assert [i.label for i in example.items] == ["a", "b"]


async def test_get_missing_raises_not_found() -> None:
    examples = InMemoryExampleRepository()
    with pytest.raises(ExampleNotFoundError):
        await GetExampleUseCase(examples).execute(uuid4())


async def test_list_examples() -> None:
    examples = InMemoryExampleRepository()
    tenant_id = uuid4()
    await examples.add(Example(tenant_id=tenant_id, name="A"))
    await examples.add(Example(tenant_id=tenant_id, name="B"))
    page = await ListExamplesUseCase(examples).execute(OffsetParams(page=1, page_size=20))
    assert page.total == 2


async def test_update_omitted_fields_are_unchanged() -> None:
    examples = InMemoryExampleRepository()
    masters = InMemoryMasterExampleRepository()
    tenant_id = uuid4()
    example = await CreateExampleUseCase(examples, masters).execute(
        CreateExampleInput(name="A", description="d", items=["x"]), tenant_id
    )
    _, after = await UpdateExampleUseCase(examples, masters).execute(
        example.id, UpdateExampleInput(name="A2")
    )
    assert after.name == "A2"
    assert after.description == "d"  # untouched
    assert [i.label for i in after.items] == ["x"]  # untouched (UNSET sentinel)


async def test_update_unknown_id_raises_not_found() -> None:
    examples = InMemoryExampleRepository()
    masters = InMemoryMasterExampleRepository()
    with pytest.raises(ExampleNotFoundError):
        await UpdateExampleUseCase(examples, masters).execute(
            uuid4(), UpdateExampleInput(name="new name")
        )


async def test_update_description_is_applied_when_provided() -> None:
    examples = InMemoryExampleRepository()
    masters = InMemoryMasterExampleRepository()
    tenant_id = uuid4()
    example = await CreateExampleUseCase(examples, masters).execute(
        CreateExampleInput(name="A", description="old"), tenant_id
    )
    _, after = await UpdateExampleUseCase(examples, masters).execute(
        example.id, UpdateExampleInput(description="new description")
    )
    assert after.description == "new description"


async def test_update_items_replaces_wholesale() -> None:
    examples = InMemoryExampleRepository()
    masters = InMemoryMasterExampleRepository()
    tenant_id = uuid4()
    example = await CreateExampleUseCase(examples, masters).execute(
        CreateExampleInput(name="A", items=["old1", "old2"]), tenant_id
    )
    _, after = await UpdateExampleUseCase(examples, masters).execute(
        example.id, UpdateExampleInput(items=["new1"])
    )
    assert [i.label for i in after.items] == ["new1"]


async def test_update_explicit_null_master_example_id_clears_it() -> None:
    examples = InMemoryExampleRepository()
    masters = InMemoryMasterExampleRepository()
    tenant_id = uuid4()
    master = await CreateMasterExampleUseCase(masters).execute(
        CreateMasterExampleInput(name="Default Currency"), tenant_id
    )
    example = await CreateExampleUseCase(examples, masters).execute(
        CreateExampleInput(name="A", master_example_id=master.id), tenant_id
    )
    _, after = await UpdateExampleUseCase(examples, masters).execute(
        example.id, UpdateExampleInput(master_example_id=None)
    )
    assert after.master_example_id is None


async def test_update_to_unknown_master_example_id_raises() -> None:
    examples = InMemoryExampleRepository()
    masters = InMemoryMasterExampleRepository()
    tenant_id = uuid4()
    example = await CreateExampleUseCase(examples, masters).execute(
        CreateExampleInput(name="A"), tenant_id
    )
    with pytest.raises(MasterExampleNotFoundError):
        await UpdateExampleUseCase(examples, masters).execute(
            example.id, UpdateExampleInput(master_example_id=uuid4())
        )


async def test_delete_returns_before_state_and_removes_it() -> None:
    examples = InMemoryExampleRepository()
    masters = InMemoryMasterExampleRepository()
    tenant_id = uuid4()
    example = await CreateExampleUseCase(examples, masters).execute(
        CreateExampleInput(name="A"), tenant_id
    )
    before = await DeleteExampleUseCase(examples).execute(example.id)
    assert before.name == "A"
    with pytest.raises(ExampleNotFoundError):
        await GetExampleUseCase(examples).execute(example.id)


async def test_delete_unknown_id_raises_not_found() -> None:
    examples = InMemoryExampleRepository()
    with pytest.raises(ExampleNotFoundError):
        await DeleteExampleUseCase(examples).execute(uuid4())
