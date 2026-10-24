"""Hostile and malformed input must produce a clean 4xx envelope, never a 500 or a traceback."""

import uuid

import httpx
import pytest

from tests.e2e.conftest import API, Api, Tokens, error_code, expect

LOGIN = "/auth/login"


@pytest.mark.parametrize(
    ("content", "content_type"),
    [
        (b"a=b", "application/x-www-form-urlencoded"),
        (b"hello", "text/plain"),
        (b"\xff\xfe\x00\x01 not json", "application/octet-stream"),
        (b'{"email": ', "application/json"),
        (b"", "application/json"),
    ],
)
def test_non_json_or_malformed_bodies_are_422_not_500(
    api: Api, content: bytes, content_type: str
) -> None:
    response = api.post(LOGIN, content=content, headers={"Content-Type": content_type})
    expect(response, 422, "validation_error")


def test_a_five_megabyte_body_does_not_crash_the_server(api: Api) -> None:
    response = api.post(
        LOGIN, content=b"x" * (5 * 1024 * 1024), headers={"Content-Type": "application/json"}
    )
    assert response.status_code < 500


def test_validation_errors_never_reflect_the_request_back(api: Api) -> None:
    response = api.post(
        LOGIN, json={"email": "not-an-email", "password": "SentinelPassw0rd!", "tenantId": "x"}
    )
    expect(response, 422)
    assert "SentinelPassw0rd!" not in response.text
    assert "traceback" not in response.text.lower()


def test_unsupported_methods_are_405(api: Api, admin: Tokens) -> None:
    expect(api.request("PUT", f"/users/{uuid.uuid4()}", admin.access, json={}), 405)
    expect(api.request("PATCH", "/audit-logs", admin.access, json={}), 405)


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Timezone": "Mars/Olympus"},
        {"X-Timezone": "../../etc/passwd"},
        {"Accept-Language": "xx-YY;q=0.9,id;q=0.8"},
        {"Accept-Language": "%%%"},
        {"Accept-Language": "*"},
        {"Accept-Language": "a" * 4000},
        {"X-Request-ID": "x" * 2000},
    ],
)
def test_hostile_negotiation_headers_never_500(
    api: Api, admin: Tokens, headers: dict[str, str]
) -> None:
    assert api.get("/users", admin.access, headers=headers).status_code < 500


def test_absurdly_long_paths_and_tokens_never_500(admin: Tokens) -> None:
    long_path = httpx.get(
        f"{API}/users/{'a' * 5000}", headers={"Authorization": f"Bearer {admin.access}"}, timeout=20
    )
    long_token = httpx.get(
        f"{API}/users", headers={"Authorization": "Bearer " + "a" * 20000}, timeout=20
    )
    assert long_path.status_code < 500
    assert long_token.status_code < 500


@pytest.mark.parametrize(
    "params",
    [
        {"page": "1;DROP TABLE sys_users"},
        {"page": "1 OR 1=1"},
        {"page_size": "200'--"},
    ],
)
def test_sqlish_query_parameters_are_rejected_as_validation_errors(
    api: Api, admin: Tokens, params: dict[str, str]
) -> None:
    expect(api.get("/users", admin.access, params=params), 422, "validation_error")


def test_error_responses_are_always_the_standard_envelope(api: Api, admin: Tokens) -> None:
    for response in (
        api.get("/users/not-a-uuid", admin.access),
        api.get(f"/users/{uuid.uuid4()}", admin.access),
        api.get("/nope-not-a-route"),
    ):
        body = response.json()
        assert set(body) == {"error", "meta"}
        assert body["error"]["code"] and body["error"]["message"]
        assert error_code(response)
