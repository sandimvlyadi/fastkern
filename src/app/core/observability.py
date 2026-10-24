"""OpenTelemetry tracing + Prometheus metrics wiring."""

import secrets
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Depends, FastAPI, Header
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_fastapi_instrumentator import Instrumentator

from app.core.config import Settings
from app.core.db import _engine
from app.core.exceptions import UnauthorizedError
from app.core.logging import get_logger

logger = get_logger(__name__)


def _require_metrics_token(expected: str) -> Callable[..., Awaitable[None]]:
    async def _check(authorization: str | None = Header(default=None)) -> None:
        scheme, _, supplied = (authorization or "").partition(" ")
        # compare_digest on bytes: constant-time, and safe for non-ASCII input.
        if scheme.lower() != "bearer" or not secrets.compare_digest(
            supplied.encode(), expected.encode()
        ):
            raise UnauthorizedError()

    return _check


def warn_if_metrics_are_unauthenticated(settings: Settings) -> None:
    """Production serves /metrics without auth when METRICS_AUTH_TOKEN is unset. That is a
    supported choice when the reverse proxy keeps /metrics off the public listener, but it must
    be visible in the logs: on a public listener it exposes every route and its traffic."""
    if settings.is_production and settings.enable_metrics and not settings.metrics_auth_token:
        logger.warning(
            "metrics_endpoint_unauthenticated",
            hint="set METRICS_AUTH_TOKEN, or make sure the reverse proxy blocks /metrics",
        )


def setup_observability(app: FastAPI, settings: Settings) -> None:
    if settings.enable_metrics:
        expose_kwargs: dict[str, Any] = {}
        token = (
            settings.metrics_auth_token.get_secret_value() if settings.metrics_auth_token else ""
        )
        if token:
            # Off by default so a local Prometheus scrape just works; set METRICS_AUTH_TOKEN (or
            # keep /metrics off the public listener at the reverse proxy) in production.
            expose_kwargs["dependencies"] = [Depends(_require_metrics_token(token))]
        Instrumentator().instrument(app).expose(
            app, endpoint="/metrics", include_in_schema=False, **expose_kwargs
        )

    if settings.enable_tracing:
        provider = TracerProvider(
            resource=Resource.create({SERVICE_NAME: settings.otel_service_name})
        )
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=f"{settings.otel_exporter_otlp_endpoint}/v1/traces")
            )
        )
        trace.set_tracer_provider(provider)
        FastAPIInstrumentor.instrument_app(app)
        SQLAlchemyInstrumentor().instrument(engine=_engine.sync_engine)
