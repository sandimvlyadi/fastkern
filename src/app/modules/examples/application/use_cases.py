"""Use cases for the `examples` module — one class per use case."""

from dataclasses import replace
from uuid import UUID

from app.core.pagination import OffsetPage, OffsetParams
from app.modules.examples.application.dto import _UNSET, CreateExampleInput, UpdateExampleInput
from app.modules.examples.domain.entities import Example, ExampleItem
from app.modules.examples.domain.exceptions import ExampleNotFoundError
from app.modules.examples.domain.ports import ExampleRepositoryPort
from app.modules.master_examples.domain.exceptions import MasterExampleNotFoundError
from app.modules.master_examples.domain.ports import MasterExampleRepositoryPort


async def _ensure_master_example_exists(
    master_examples: MasterExampleRepositoryPort, master_example_id: UUID | None
) -> None:
    if master_example_id is not None and await master_examples.get_by_id(master_example_id) is None:
        raise MasterExampleNotFoundError(example_id=str(master_example_id))


class CreateExampleUseCase:
    def __init__(
        self,
        examples: ExampleRepositoryPort,
        master_examples: MasterExampleRepositoryPort,
    ) -> None:
        self._examples = examples
        self._master_examples = master_examples

    async def execute(self, data: CreateExampleInput, tenant_id: UUID) -> Example:
        await _ensure_master_example_exists(self._master_examples, data.master_example_id)
        entity = Example(
            tenant_id=tenant_id,
            name=data.name,
            description=data.description,
            master_example_id=data.master_example_id,
            items=[ExampleItem(label=label) for label in data.items],
        )
        return await self._examples.add(entity)


class ListExamplesUseCase:
    def __init__(self, examples: ExampleRepositoryPort) -> None:
        self._examples = examples

    async def execute(self, params: OffsetParams) -> OffsetPage[Example]:
        return await self._examples.paginate(params)


class GetExampleUseCase:
    def __init__(self, examples: ExampleRepositoryPort) -> None:
        self._examples = examples

    async def execute(self, example_id: UUID) -> Example:
        example = await self._examples.get_by_id(example_id)
        if example is None:
            raise ExampleNotFoundError(example_id=str(example_id))
        return example


class UpdateExampleUseCase:
    def __init__(
        self,
        examples: ExampleRepositoryPort,
        master_examples: MasterExampleRepositoryPort,
    ) -> None:
        self._examples = examples
        self._master_examples = master_examples

    async def execute(self, example_id: UUID, data: UpdateExampleInput) -> tuple[Example, Example]:
        """Returns (before, after) for the caller's audit-log diff."""
        example = await self._examples.get_by_id(example_id)
        if example is None:
            raise ExampleNotFoundError(example_id=str(example_id))
        before = replace(example, items=list(example.items))
        if data.name is not None:
            example.name = data.name
        if data.description is not None:
            example.description = data.description
        if (
            data.master_example_id is not _UNSET
            and data.master_example_id != example.master_example_id
        ):
            await _ensure_master_example_exists(self._master_examples, data.master_example_id)
            example.master_example_id = data.master_example_id
        if data.items is not _UNSET:
            example.items = [ExampleItem(label=label) for label in (data.items or [])]
        after = await self._examples.save(example)
        return before, after


class DeleteExampleUseCase:
    def __init__(self, examples: ExampleRepositoryPort) -> None:
        self._examples = examples

    async def execute(self, example_id: UUID) -> Example:
        """Soft-deletes the example and returns it as it was, for the audit log."""
        example = await self._examples.get_by_id(example_id)
        if example is None:
            raise ExampleNotFoundError(example_id=str(example_id))
        before = replace(example, items=list(example.items))
        await self._examples.delete(example_id)
        return before
