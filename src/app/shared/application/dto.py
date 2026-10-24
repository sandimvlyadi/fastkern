"""Shared application-layer building blocks."""

from pydantic import BaseModel, ConfigDict


class CamelModel(BaseModel):
    """Base for request/response DTOs: accepts and emits camelCase over the
    wire while keeping snake_case field names in Python."""

    model_config = ConfigDict(
        alias_generator=lambda field_name: "".join(
            word.capitalize() if i else word for i, word in enumerate(field_name.split("_"))
        ),
        populate_by_name=True,
        from_attributes=True,
    )
