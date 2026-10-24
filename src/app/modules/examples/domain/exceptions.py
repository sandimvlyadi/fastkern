from app.core.exceptions import NotFoundError


class ExampleNotFoundError(NotFoundError):
    code = "example_not_found"
    key = "example.not_found"
    message = "Example not found."
