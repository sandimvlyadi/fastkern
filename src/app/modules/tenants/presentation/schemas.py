from uuid import UUID

from pydantic import ConfigDict, Field

from app.shared.application.dto import CamelModel


class TenantResponse(CamelModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": "9c858901-8a57-4791-81fe-4c455b099bc9",
                    "name": "Acme Corporation",
                    "slug": "acme-corp",
                    "isActive": True,
                }
            ]
        }
    )

    id: UUID
    name: str
    slug: str
    is_active: bool


class CreateTenantRequest(CamelModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"name": "Acme Corporation", "slug": "acme-corp"}]}
    )

    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9-]+$")


class UpdateTenantRequest(CamelModel):
    """Partial update: omitted fields are unchanged. Activation is not set
    here — use the activate/deactivate endpoints."""

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"name": "Acme Holdings", "slug": "acme-holdings"}]}
    )

    name: str | None = Field(default=None, min_length=1, max_length=255)
    slug: str | None = Field(default=None, min_length=1, max_length=100, pattern=r"^[a-z0-9-]+$")
