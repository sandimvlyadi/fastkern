from uuid import UUID

from pydantic import ConfigDict, Field

from app.shared.application.dto import CamelModel


class MasterExampleResponse(CamelModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": "9c858901-8a57-4791-81fe-4c455b099bc9",
                    "name": "Default Currency",
                    "description": "Reference row used across example transactions.",
                }
            ]
        }
    )

    id: UUID
    name: str
    description: str


class CreateMasterExampleRequest(CamelModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "name": "Default Currency",
                    "description": "Reference row used across example transactions.",
                }
            ]
        }
    )

    name: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=1000)


class UpdateMasterExampleRequest(CamelModel):
    """Partial update: omitted fields are unchanged."""

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"name": "Default Currency (Legacy)"}]}
    )

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
