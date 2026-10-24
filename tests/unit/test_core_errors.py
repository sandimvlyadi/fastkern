import json

import pytest
from fastapi import FastAPI, status
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request

from app.core.errors import error_responses, register_exception_handlers
from app.core.exceptions import (
    AlreadyExistsError,
    AppError,
    ExternalServiceError,
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
)
from app.core.response import ErrorEnvelope, error_envelope, success_envelope


class _ChildNotFoundError(NotFoundError):
    code = "child_not_found"
    message = "Child not found."


class _SiblingNotFoundError(NotFoundError):
    code = "sibling_not_found"
    message = "Sibling not found."


class _FirstDuplicateCodeError(NotFoundError):
    code = "duplicate_code"
    message = "First."


class _SecondDuplicateCodeError(NotFoundError):
    code = "duplicate_code"
    message = "Second."


def _request() -> Request:
    return Request({"type": "http", "method": "GET", "path": "/", "headers": []})


@pytest.fixture
def app() -> FastAPI:
    application = FastAPI()
    register_exception_handlers(application)
    return application


def test_app_error_defaults() -> None:
    err = AppError()
    assert err.code == "internal_error"
    assert err.message == "An unexpected error occurred."
    assert err.context == {}


def test_app_error_message_override_and_context() -> None:
    err = NotFoundError("custom message", resource_id="abc")
    assert err.message == "custom message"
    assert err.context == {"resource_id": "abc"}


def test_success_envelope_with_pagination() -> None:
    envelope = success_envelope([1, 2], request_id="req-1", pagination={"page": 1})
    assert envelope == {"data": [1, 2], "meta": {"request_id": "req-1", "pagination": {"page": 1}}}


def test_success_envelope_without_pagination() -> None:
    envelope = success_envelope({"a": 1}, request_id="req-1")
    assert envelope["meta"]["pagination"] is None


def test_error_envelope_with_details() -> None:
    envelope = error_envelope(code="x", message="y", request_id="req-1", details={"field": "bad"})
    assert envelope == {
        "error": {"code": "x", "message": "y", "details": {"field": "bad"}},
        "meta": {"request_id": "req-1", "pagination": None},
    }


def test_error_envelope_without_details() -> None:
    envelope = error_envelope(code="x", message="y", request_id="req-1")
    assert envelope["error"]["details"] is None


async def test_handle_app_error_4xx(app: FastAPI) -> None:
    handler = app.exception_handlers[AppError]
    response = await handler(_request(), _ChildNotFoundError(resource_id="42"))
    assert response.status_code == status.HTTP_404_NOT_FOUND
    body = json.loads(response.body)
    assert body["error"]["code"] == "child_not_found"
    assert body["error"]["details"] == {"resource_id": "42"}


async def test_handle_app_error_5xx_logs_exception(app: FastAPI) -> None:
    handler = app.exception_handlers[AppError]
    response = await handler(_request(), ExternalServiceError())
    assert response.status_code == status.HTTP_502_BAD_GATEWAY


async def test_handle_app_error_base_class_defaults_to_500(app: FastAPI) -> None:
    handler = app.exception_handlers[AppError]
    response = await handler(_request(), AppError())
    assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (NotFoundError(), status.HTTP_404_NOT_FOUND),
        (AlreadyExistsError(), status.HTTP_409_CONFLICT),
        (UnauthorizedError(), status.HTTP_401_UNAUTHORIZED),
        (ForbiddenError(), status.HTTP_403_FORBIDDEN),
        (ExternalServiceError(), status.HTTP_502_BAD_GATEWAY),
    ],
)
async def test_handle_app_error_status_by_type(
    app: FastAPI, error: AppError, expected_status: int
) -> None:
    handler = app.exception_handlers[AppError]
    response = await handler(_request(), error)
    assert response.status_code == expected_status


async def test_handle_validation_error(app: FastAPI) -> None:
    handler = app.exception_handlers[RequestValidationError]
    exc = RequestValidationError(
        [{"loc": ("body", "email"), "msg": "field required", "type": "missing"}]
    )
    response = await handler(_request(), exc)
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    body = json.loads(response.body)
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["details"]["errors"]


async def test_handle_http_exception(app: FastAPI) -> None:
    handler = app.exception_handlers[StarletteHTTPException]
    response = await handler(_request(), StarletteHTTPException(status_code=404, detail="nope"))
    assert response.status_code == 404
    body = json.loads(response.body)
    assert body["error"]["code"] == "http_error"
    assert body["error"]["message"] == "nope"


async def test_handle_unexpected_error(app: FastAPI) -> None:
    handler = app.exception_handlers[Exception]
    response = await handler(_request(), RuntimeError("boom"))
    assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    body = json.loads(response.body)
    assert body["error"]["code"] == "internal_error"


def test_error_responses_groups_multiple_classes_under_one_status() -> None:
    responses = error_responses(_ChildNotFoundError, _SiblingNotFoundError, AlreadyExistsError)

    assert set(responses) == {status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT}

    not_found = responses[status.HTTP_404_NOT_FOUND]
    assert not_found["model"] is ErrorEnvelope
    examples = not_found["content"]["application/json"]["examples"]
    assert set(examples) == {"child_not_found", "sibling_not_found"}
    assert examples["child_not_found"]["value"]["error"]["code"] == "child_not_found"
    assert examples["child_not_found"]["value"]["meta"]["pagination"] is None

    conflict = responses[status.HTTP_409_CONFLICT]
    assert set(conflict["content"]["application/json"]["examples"]) == {"already_exists"}


def test_error_responses_without_validation_flag_has_no_422() -> None:
    responses = error_responses(NotFoundError)
    assert status.HTTP_422_UNPROCESSABLE_CONTENT not in responses


def test_error_responses_validation_flag_documents_real_envelope_shape() -> None:
    responses = error_responses(validation=True)
    validation_response = responses[status.HTTP_422_UNPROCESSABLE_CONTENT]
    assert validation_response["model"] is ErrorEnvelope
    example = validation_response["content"]["application/json"]["example"]
    assert example["error"]["code"] == "validation_error"
    assert example["error"]["details"]["errors"]


def test_error_responses_validation_response_not_shared_between_calls() -> None:
    first = error_responses(validation=True)[status.HTTP_422_UNPROCESSABLE_CONTENT]
    second = error_responses(validation=True)[status.HTTP_422_UNPROCESSABLE_CONTENT]
    assert first is not second


def test_error_responses_raises_on_duplicate_code_same_status() -> None:
    with pytest.raises(ValueError, match="duplicate_code"):
        error_responses(_FirstDuplicateCodeError, _SecondDuplicateCodeError)


def test_error_responses_same_class_passed_twice_is_not_a_collision() -> None:
    responses = error_responses(NotFoundError, NotFoundError)
    examples = responses[status.HTTP_404_NOT_FOUND]["content"]["application/json"]["examples"]
    assert set(examples) == {"not_found"}
