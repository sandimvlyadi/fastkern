from uuid import uuid4

import pytest

from app.core.pagination import OffsetParams
from app.modules.master_examples.application.dto import (
    CreateMasterExampleInput,
    UpdateMasterExampleInput,
)
from app.modules.master_examples.application.use_cases import (
    CreateMasterExampleUseCase,
    DeleteMasterExampleUseCase,
    GetMasterExampleUseCase,
    ListMasterExamplesUseCase,
    UpdateMasterExampleUseCase,
)
from app.modules.master_examples.domain.entities import MasterExample
from app.modules.master_examples.domain.exceptions import (
    MasterExampleAlreadyExistsError,
    MasterExampleNotFoundError,
)
from tests.fakes import InMemoryMasterExampleRepository


async def test_create_master_example_success() -> None:
    examples = InMemoryMasterExampleRepository()
    tenant_id = uuid4()
    example = await CreateMasterExampleUseCase(examples).execute(
        CreateMasterExampleInput(name="Default Currency", description="A reference row."),
        tenant_id,
    )
    assert example.name == "Default Currency"
    assert example.tenant_id == tenant_id


async def test_create_duplicate_name() -> None:
    examples = InMemoryMasterExampleRepository()
    tenant_id = uuid4()
    await CreateMasterExampleUseCase(examples).execute(
        CreateMasterExampleInput(name="Default Currency"), tenant_id
    )
    with pytest.raises(MasterExampleAlreadyExistsError):
        await CreateMasterExampleUseCase(examples).execute(
            CreateMasterExampleInput(name="Default Currency"), tenant_id
        )


async def test_get_missing_raises_not_found() -> None:
    examples = InMemoryMasterExampleRepository()
    with pytest.raises(MasterExampleNotFoundError):
        await GetMasterExampleUseCase(examples).execute(uuid4())


async def test_list_master_examples() -> None:
    examples = InMemoryMasterExampleRepository()
    tenant_id = uuid4()
    await examples.add(MasterExample(tenant_id=tenant_id, name="A"))
    await examples.add(MasterExample(tenant_id=tenant_id, name="B"))
    page = await ListMasterExamplesUseCase(examples).execute(OffsetParams(page=1, page_size=20))
    assert page.total == 2


async def test_update_name_to_existing_other_record() -> None:
    examples = InMemoryMasterExampleRepository()
    tenant_id = uuid4()
    first = await CreateMasterExampleUseCase(examples).execute(
        CreateMasterExampleInput(name="A"), tenant_id
    )
    second = await CreateMasterExampleUseCase(examples).execute(
        CreateMasterExampleInput(name="B"), tenant_id
    )
    with pytest.raises(MasterExampleAlreadyExistsError):
        await UpdateMasterExampleUseCase(examples).execute(
            second.id, UpdateMasterExampleInput(name=first.name)
        )


async def test_update_unknown_id_raises_not_found() -> None:
    examples = InMemoryMasterExampleRepository()
    with pytest.raises(MasterExampleNotFoundError):
        await UpdateMasterExampleUseCase(examples).execute(
            uuid4(), UpdateMasterExampleInput(name="new name")
        )


async def test_update_changes_name_and_description() -> None:
    examples = InMemoryMasterExampleRepository()
    tenant_id = uuid4()
    example = await CreateMasterExampleUseCase(examples).execute(
        CreateMasterExampleInput(name="A", description="old"), tenant_id
    )
    before, after = await UpdateMasterExampleUseCase(examples).execute(
        example.id, UpdateMasterExampleInput(name="A2", description="new")
    )
    assert before.name == "A"
    assert after.name == "A2"
    assert after.description == "new"


async def test_delete_returns_before_state_and_removes_it() -> None:
    examples = InMemoryMasterExampleRepository()
    tenant_id = uuid4()
    example = await CreateMasterExampleUseCase(examples).execute(
        CreateMasterExampleInput(name="A"), tenant_id
    )
    before = await DeleteMasterExampleUseCase(examples).execute(example.id)
    assert before.name == "A"
    with pytest.raises(MasterExampleNotFoundError):
        await GetMasterExampleUseCase(examples).execute(example.id)


async def test_delete_unknown_id_raises_not_found() -> None:
    examples = InMemoryMasterExampleRepository()
    with pytest.raises(MasterExampleNotFoundError):
        await DeleteMasterExampleUseCase(examples).execute(uuid4())
