"""Documents the globally-negotiated request headers in `/docs`/`/redoc`.

`Accept-Language` and `X-Timezone` are read by `RequestContextMiddleware`, not declared as
`Header(...)` parameters, so FastAPI cannot document them. Declaring them on every route would
drift from what the middleware does; this patches the generated schema once instead, as
FastAPI's docs suggest for parameters common to every route:
https://fastapi.tiangolo.com/how-to/extending-openapi/
"""

from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from app.core.config import Settings
from app.core.middleware.request_context import ACCEPT_LANGUAGE_HEADER, TIMEZONE_HEADER


def _common_header_parameters(settings: Settings) -> list[dict[str, Any]]:
    return [
        {
            "name": ACCEPT_LANGUAGE_HEADER,
            "in": "header",
            "required": False,
            "schema": {"type": "string", "default": settings.default_locale},
            "description": (
                "Negotiates the locale for translated error/notification "
                f"messages. Supported: {', '.join(settings.supported_locales)}. "
                f"Falls back to '{settings.default_locale}' if omitted or "
                "unsupported."
            ),
        },
        {
            "name": TIMEZONE_HEADER,
            "in": "header",
            "required": False,
            "schema": {"type": "string", "default": settings.default_timezone},
            "description": (
                "IANA timezone name (e.g. 'Asia/Jakarta') used only at "
                "human-rendering edges (PDF/email/CSV) — response bodies "
                f"stay UTC. Falls back to '{settings.default_timezone}' if "
                "omitted or unrecognized."
            ),
        },
    ]


def register_common_header_docs(app: FastAPI, settings: Settings) -> None:
    """Overrides `app.openapi()` so every documented operation lists
    `Accept-Language`/`X-Timezone` as header parameters — purely
    documentation, the middleware's actual header handling is unchanged."""

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema

        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
        # Shared (not copied) across operations: the schema is only serialized afterwards.
        common_params = _common_header_parameters(settings)
        for path_item in schema.get("paths", {}).values():
            for operation in path_item.values():
                if not isinstance(operation, dict) or "responses" not in operation:
                    continue  # skip non-operation keys (e.g. a shared "parameters" list)
                existing_names = {p.get("name") for p in operation.setdefault("parameters", [])}
                operation["parameters"].extend(
                    param for param in common_params if param["name"] not in existing_names
                )

        app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = custom_openapi  # type: ignore[method-assign]
