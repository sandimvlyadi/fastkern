from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI

from app.core import observability
from app.core.config import get_settings


@pytest.fixture
def app() -> FastAPI:
    return FastAPI()


def test_metrics_only(monkeypatch: pytest.MonkeyPatch, app: FastAPI) -> None:
    instrumentator = MagicMock()
    monkeypatch.setattr(observability, "Instrumentator", MagicMock(return_value=instrumentator))
    trace_mock = MagicMock()
    monkeypatch.setattr(observability, "trace", trace_mock)

    settings = get_settings().model_copy(update={"enable_metrics": True, "enable_tracing": False})
    observability.setup_observability(app, settings)

    instrumentator.instrument.assert_called_once_with(app)
    instrumentator.instrument.return_value.expose.assert_called_once_with(
        app, endpoint="/metrics", include_in_schema=False
    )
    trace_mock.set_tracer_provider.assert_not_called()


def test_tracing_only(monkeypatch: pytest.MonkeyPatch, app: FastAPI) -> None:
    monkeypatch.setattr(observability, "Instrumentator", MagicMock())
    provider_instance = MagicMock()
    tracer_provider_cls = MagicMock(return_value=provider_instance)
    monkeypatch.setattr(observability, "TracerProvider", tracer_provider_cls)
    batch_processor_cls = MagicMock()
    monkeypatch.setattr(observability, "BatchSpanProcessor", batch_processor_cls)
    otlp_exporter_cls = MagicMock()
    monkeypatch.setattr(observability, "OTLPSpanExporter", otlp_exporter_cls)
    trace_mock = MagicMock()
    monkeypatch.setattr(observability, "trace", trace_mock)
    fastapi_instrumentor = MagicMock()
    monkeypatch.setattr(observability, "FastAPIInstrumentor", fastapi_instrumentor)
    sqlalchemy_instrumentor_instance = MagicMock()
    sqlalchemy_instrumentor_cls = MagicMock(return_value=sqlalchemy_instrumentor_instance)
    monkeypatch.setattr(observability, "SQLAlchemyInstrumentor", sqlalchemy_instrumentor_cls)

    settings = get_settings().model_copy(
        update={
            "enable_metrics": False,
            "enable_tracing": True,
            "otel_exporter_otlp_endpoint": "http://collector:4318",
        }
    )
    observability.setup_observability(app, settings)

    otlp_exporter_cls.assert_called_once_with(endpoint="http://collector:4318/v1/traces")
    batch_processor_cls.assert_called_once_with(otlp_exporter_cls.return_value)
    provider_instance.add_span_processor.assert_called_once_with(batch_processor_cls.return_value)
    trace_mock.set_tracer_provider.assert_called_once_with(provider_instance)
    fastapi_instrumentor.instrument_app.assert_called_once_with(app)
    sqlalchemy_instrumentor_instance.instrument.assert_called_once()


def test_both_disabled_does_nothing(monkeypatch: pytest.MonkeyPatch, app: FastAPI) -> None:
    instrumentator_cls = MagicMock()
    monkeypatch.setattr(observability, "Instrumentator", instrumentator_cls)
    trace_mock = MagicMock()
    monkeypatch.setattr(observability, "trace", trace_mock)

    settings = get_settings().model_copy(update={"enable_metrics": False, "enable_tracing": False})
    observability.setup_observability(app, settings)

    instrumentator_cls.assert_not_called()
    trace_mock.set_tracer_provider.assert_not_called()


def test_production_without_a_metrics_token_warns_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from pydantic import SecretStr

    events: list[str] = []

    class _Capture:
        def warning(self, event: str, **_: object) -> None:
            events.append(event)

    monkeypatch.setattr(observability, "logger", _Capture())
    for is_production, enable_metrics, token in [
        (True, True, None),  # the one that warns
        (False, True, None),
        (True, False, None),
        (True, True, SecretStr("scrape-token")),
    ]:
        observability.warn_if_metrics_are_unauthenticated(
            SimpleNamespace(  # type: ignore[arg-type]
                is_production=is_production,
                enable_metrics=enable_metrics,
                metrics_auth_token=token,
            )
        )
    assert events == ["metrics_endpoint_unauthenticated"]
