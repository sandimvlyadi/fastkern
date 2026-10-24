import pytest
from fastapi import FastAPI, Header

from app.core.config import get_settings
from app.core.openapi import register_common_header_docs


def _build_app() -> FastAPI:
    app = FastAPI()
    register_common_header_docs(app, get_settings())

    @app.get("/widgets")
    async def list_widgets() -> dict[str, str]:
        return {}

    @app.get("/widgets/{widget_id}", include_in_schema=False)
    async def get_widget(widget_id: str) -> dict[str, str]:
        return {}

    @app.get("/already-declared")
    async def already_declared(
        accept_language: str | None = Header(default=None),
    ) -> dict[str, str]:
        return {}

    return app


def _header_param_names(operation: dict) -> set[str]:
    return {p["name"] for p in operation["parameters"] if p["in"] == "header"}


def test_common_headers_are_documented_on_every_operation() -> None:
    schema = _build_app().openapi()
    operation = schema["paths"]["/widgets"]["get"]
    assert _header_param_names(operation) == {"Accept-Language", "X-Timezone"}


def test_excluded_from_schema_routes_are_unaffected() -> None:
    schema = _build_app().openapi()
    assert "/widgets/{widget_id}" not in schema["paths"]


def test_does_not_duplicate_an_already_declared_header_param() -> None:
    schema = _build_app().openapi()
    operation = schema["paths"]["/already-declared"]["get"]
    header_names = [p["name"] for p in operation["parameters"] if p["in"] == "header"]
    # FastAPI's `Header(...)` auto-derives the exact same "Accept-Language"
    # name from the `accept_language` parameter, so our injected copy must
    # be skipped rather than appended a second time.
    assert header_names.count("Accept-Language") == 1
    assert header_names.count("X-Timezone") == 1


def test_openapi_schema_is_cached_after_first_call() -> None:
    app = _build_app()
    first = app.openapi()
    second = app.openapi()
    assert first is second


def test_skips_non_operation_keys_in_a_path_item(monkeypatch: pytest.MonkeyPatch) -> None:
    """A path item can carry a shared `parameters` list alongside its
    per-method operations (OpenAPI allows it, even though FastAPI's own
    `get_openapi()` never emits one) — the patching loop must skip that
    key rather than crash on it or inject headers into it."""
    app = FastAPI()
    register_common_header_docs(app, get_settings())

    @app.get("/widgets")
    async def list_widgets() -> dict[str, str]:
        return {}

    fake_schema = {
        "paths": {
            "/widgets": {
                "parameters": [{"name": "shared", "in": "header"}],
                "get": {"responses": {"200": {"description": "OK"}}},
            }
        }
    }
    monkeypatch.setattr("app.core.openapi.get_openapi", lambda **kwargs: fake_schema)

    schema = app.openapi()

    get_operation = schema["paths"]["/widgets"]["get"]
    assert _header_param_names(get_operation) == {"Accept-Language", "X-Timezone"}
    assert schema["paths"]["/widgets"]["parameters"] == [{"name": "shared", "in": "header"}]
