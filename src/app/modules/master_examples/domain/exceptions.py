from app.core.exceptions import AlreadyExistsError, NotFoundError


class MasterExampleNotFoundError(NotFoundError):
    code = "master_example_not_found"
    key = "master_example.not_found"
    message = "Master example not found."


class MasterExampleAlreadyExistsError(AlreadyExistsError):
    code = "master_example_already_exists"
    key = "master_example.already_exists"
    message = "A master example with this name already exists."
