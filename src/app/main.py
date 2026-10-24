"""ASGI entrypoint / composition of the FastAPI application.

Run with: `uvicorn app.main:app --app-dir src` (or `make dev`).
"""

from fastapi import FastAPI, status
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.health import get_readiness
from app.core.logging import configure_logging, get_logger
from app.core.middleware import RequestContextMiddleware, SecurityHeadersMiddleware
from app.core.middleware.api_traffic_log import ApiTrafficLogMiddleware
from app.core.middleware.body_limit import MaxRequestBodyMiddleware
from app.core.middleware.request_context import REQUEST_ID_HEADER
from app.core.observability import setup_observability, warn_if_metrics_are_unauthenticated
from app.core.openapi import register_common_header_docs
from app.core.rate_limit import RATE_LIMIT_RESPONSE_HEADERS
from app.modules.api_clients.presentation.router import router as api_clients_router
from app.modules.audit_log.presentation.router import router as audit_log_router
from app.modules.auth.presentation.router import router as auth_router
from app.modules.examples.presentation.router import router as examples_router
from app.modules.files.infrastructure.factory import warn_if_uploads_are_unscanned
from app.modules.files.presentation.router import public_router as files_public_router
from app.modules.files.presentation.router import router as files_router
from app.modules.master_examples.presentation.router import router as master_examples_router
from app.modules.partners.presentation.router import router as partners_router
from app.modules.posts.presentation.router import router as posts_router
from app.modules.roles.presentation.router import router as roles_router
from app.modules.tenants.presentation.router import router as tenants_router
from app.modules.users.presentation.router import router as users_router

settings = get_settings()
configure_logging(settings)
logger = get_logger(__name__)


def create_app(app_settings: Settings | None = None) -> FastAPI:
    settings = app_settings or get_settings()
    warn_if_uploads_are_unscanned(settings)
    warn_if_metrics_are_unauthenticated(settings)
    docs_enabled = settings.api_docs_enabled
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        debug=settings.debug,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )

    # Middleware order matters: Starlette applies the LAST added first, so
    # CORS (added last) wraps everything, including error responses.
    # ApiTrafficLogMiddleware reads `request.state`, so its position in this list does not matter.
    app.add_middleware(RequestContextMiddleware, settings=settings)
    app.add_middleware(SecurityHeadersMiddleware, settings=settings)
    app.add_middleware(ApiTrafficLogMiddleware)
    # Refuses an oversized single-request upload before the multipart body is spooled to disk.
    # One chunk plus 64 KiB for multipart framing and part headers.
    app.add_middleware(
        MaxRequestBodyMiddleware,
        method="POST",
        path=f"{settings.api_v1_prefix}/files",
        max_bytes=settings.upload_chunk_max_bytes + 64 * 1024,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        # Not CORS-safelisted, so a browser client on another origin can't
        # read them (e.g. to honour Retry-After after a 429) unless exposed.
        expose_headers=[REQUEST_ID_HEADER, *RATE_LIMIT_RESPONSE_HEADERS],
    )

    register_exception_handlers(app)
    register_common_header_docs(app, settings)
    setup_observability(app, settings)

    api_prefix = settings.api_v1_prefix
    app.include_router(auth_router, prefix=api_prefix)
    app.include_router(users_router, prefix=api_prefix)
    app.include_router(roles_router, prefix=api_prefix)
    app.include_router(audit_log_router, prefix=api_prefix)
    app.include_router(tenants_router, prefix=api_prefix)
    app.include_router(master_examples_router, prefix=api_prefix)
    app.include_router(examples_router, prefix=api_prefix)
    app.include_router(files_router, prefix=api_prefix)
    app.include_router(files_public_router, prefix=api_prefix)
    app.include_router(posts_router, prefix=api_prefix)
    app.include_router(api_clients_router, prefix=api_prefix)
    app.include_router(partners_router, prefix=api_prefix)

    @app.get("/healthz", tags=["system"], include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", tags=["system"], include_in_schema=False)
    async def readyz() -> JSONResponse:
        readiness = await get_readiness()
        status_code = (
            status.HTTP_200_OK
            if readiness["status"] == "ready"
            else status.HTTP_503_SERVICE_UNAVAILABLE
        )
        return JSONResponse(content=readiness, status_code=status_code)

    return app


app = create_app()
