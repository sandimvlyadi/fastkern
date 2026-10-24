from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CreatePostInput:
    user_id: int
    title: str
    body: str


@dataclass(frozen=True, slots=True)
class UpdatePostInput:
    title: str | None = None
    body: str | None = None
