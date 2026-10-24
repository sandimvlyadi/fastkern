"""Use cases for the `master_examples` module — one class per use case."""

from dataclasses import replace
from uuid import UUID

from app.core.pagination import OffsetPage, OffsetParams
from app.modules.master_examples.application.dto import (
    CreateMasterExampleInput,
    UpdateMasterExampleInput,
)
from app.modules.master_examples.domain.entities import MasterExample
from app.modules.master_examples.domain.exceptions import (
    MasterExampleAlreadyExistsError,
    MasterExampleNotFoundError,
)
from app.modules.master_examples.domain.ports import MasterExampleRepositoryPort


class CreateMasterExampleUseCase:
    def __init__(self, examples: MasterExampleRepositoryPort) -> None:
        self._examples = examples

    async def execute(self, data: CreateMasterExampleInput, tenant_id: UUID) -> MasterExample:
        if await self._examples.get_by_name(data.name) is not None:
            raise MasterExampleAlreadyExistsError(name=data.name)
        return await self._examples.add(
            MasterExample(tenant_id=tenant_id, name=data.name, description=data.description)
        )


class ListMasterExamplesUseCase:
    def __init__(self, examples: MasterExampleRepositoryPort) -> None:
        self._examples = examples

    async def execute(self, params: OffsetParams) -> OffsetPage[MasterExample]:
        return await self._examples.paginate(params)


class GetMasterExampleUseCase:
    def __init__(self, examples: MasterExampleRepositoryPort) -> None:
        self._examples = examples

    async def execute(self, example_id: UUID) -> MasterExample:
        example = await self._examples.get_by_id(example_id)
        if example is None:
            raise MasterExampleNotFoundError(example_id=str(example_id))
        return example


class UpdateMasterExampleUseCase:
    def __init__(self, examples: MasterExampleRepositoryPort) -> None:
        self._examples = examples

    async def execute(
        self, example_id: UUID, data: UpdateMasterExampleInput
    ) -> tuple[MasterExample, MasterExample]:
        """Returns (before, after) for the caller's audit-log diff."""
        example = await self._examples.get_by_id(example_id)
        if example is None:
            raise MasterExampleNotFoundError(example_id=str(example_id))
        before = replace(example)
        if data.name is not None and data.name != example.name:
            existing = await self._examples.get_by_name(data.name)
            if existing is not None and existing.id != example.id:
                raise MasterExampleAlreadyExistsError(name=data.name)
            example.name = data.name
        if data.description is not None:
            example.description = data.description
        after = await self._examples.save(example)
        return before, after


class DeleteMasterExampleUseCase:
    def __init__(self, examples: MasterExampleRepositoryPort) -> None:
        self._examples = examples

    async def execute(self, example_id: UUID) -> MasterExample:
        """Soft-deletes the master example and returns it as it was, for the audit log."""
        example = await self._examples.get_by_id(example_id)
        if example is None:
            raise MasterExampleNotFoundError(example_id=str(example_id))
        before = replace(example)
        await self._examples.delete(example_id)
        return before
