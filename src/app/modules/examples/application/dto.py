from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

# "Not sent in the PATCH body", as opposed to an explicit null (see UpdateExampleInput).
_UNSET: Any = object()


@dataclass(frozen=True, slots=True)
class CreateExampleInput:
    name: str
    description: str = ""
    master_example_id: UUID | None = None
    items: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class UpdateExampleInput:
    """`name`/`description` left as None are unchanged; nothing clears them.

    `master_example_id` and `items` can be cleared, so `None` is a real value for them ("clear
    the reference" / "clear the list") and "unchanged" is the `_UNSET` sentinel instead. Build
    this from `body.model_dump(exclude_unset=True)` so an omitted field arrives as `_UNSET`."""

    name: str | None = None
    description: str | None = None
    master_example_id: UUID | None = _UNSET
    items: list[str] | None = _UNSET
