from pydantic import ConfigDict, Field

from app.shared.application.dto import CamelModel


class PostResponse(CamelModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": 1,
                    "userId": 1,
                    "title": "sunt aut facere repellat provident",
                    "body": "quia et suscipit\nsuscipit recusandae",
                }
            ]
        }
    )

    id: int
    user_id: int
    title: str
    body: str


class CreatePostRequest(CamelModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [{"userId": 1, "title": "My new post", "body": "Post content goes here."}]
        }
    )

    user_id: int = Field(gt=0)
    title: str = Field(min_length=1, max_length=255)
    body: str = Field(min_length=1, max_length=5000)


class UpdatePostRequest(CamelModel):
    """Partial update: an omitted field is unchanged."""

    model_config = ConfigDict(json_schema_extra={"examples": [{"title": "Updated title"}]})

    title: str | None = Field(default=None, min_length=1, max_length=255)
    body: str | None = Field(default=None, min_length=1, max_length=5000)
