from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CreateMasterExampleInput:
    name: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class UpdateMasterExampleInput:
    """Fields left as None are unchanged."""

    name: str | None = None
    description: str | None = None
