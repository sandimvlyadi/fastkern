from uuid import UUID

from pydantic import ConfigDict, Field

from app.shared.application.dto import CamelModel


class ExampleItemResponse(CamelModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [{"id": "9c858901-8a57-4791-81fe-4c455b099bc1", "label": "priority"}]
        }
    )

    id: UUID
    label: str


class ExampleResponse(CamelModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": "9c858901-8a57-4791-81fe-4c455b099bc9",
                    "name": "Invoice #1042",
                    "description": "Example transaction referencing a master example.",
                    "masterExampleId": "9c858901-8a57-4791-81fe-4c455b099bc2",
                    "items": [{"id": "9c858901-8a57-4791-81fe-4c455b099bc1", "label": "priority"}],
                }
            ]
        }
    )

    id: UUID
    name: str
    description: str
    master_example_id: UUID | None
    items: list[ExampleItemResponse]


class CreateExampleRequest(CamelModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "name": "Invoice #1042",
                    "description": "Example transaction referencing a master example.",
                    "masterExampleId": "9c858901-8a57-4791-81fe-4c455b099bc2",
                    "items": ["priority", "reviewed"],
                }
            ]
        }
    )

    name: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=1000)
    master_example_id: UUID | None = Field(default=None)
    items: list[str] = Field(default_factory=list)


class UpdateExampleRequest(CamelModel):
    """Partial update: an omitted field is unchanged. Send `masterExampleId: null` or
    `items: []` to clear them."""

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"name": "Invoice #1042 (revised)", "items": ["priority"]}]}
    )

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    master_example_id: UUID | None = Field(default=None)
    items: list[str] | None = Field(default=None)
