import logging
from collections.abc import Generator
from uuid import uuid4

import pytest
from opentelemetry.sdk.trace import TracerProvider

from app.core.config import get_settings
from app.core.context import update_request_context
from app.core.logging import _inject_request_context, _inject_trace_context, configure_logging


@pytest.fixture(autouse=True)
def _restore_logging() -> Generator[None]:
    yield
    configure_logging(get_settings())


def test_inject_request_context_without_tenant_or_user() -> None:
    event_dict = _inject_request_context(None, "info", {})
    assert event_dict["request_id"] == "-"
    assert "tenant_id" not in event_dict
    assert "user_id" not in event_dict


def test_inject_request_context_with_tenant_and_user() -> None:
    tenant_id, user_id = uuid4(), uuid4()
    update_request_context(tenant_id=tenant_id, user_id=user_id)
    event_dict = _inject_request_context(None, "info", {})
    assert event_dict["tenant_id"] == str(tenant_id)
    assert event_dict["user_id"] == str(user_id)


def test_inject_trace_context_with_active_span() -> None:
    provider = TracerProvider()
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("span"):
        event_dict = _inject_trace_context(None, "info", {})
    assert "trace_id" in event_dict
    assert "span_id" in event_dict


def test_inject_trace_context_without_span_is_noop() -> None:
    event_dict = _inject_trace_context(None, "info", {})
    assert "trace_id" not in event_dict
    assert "span_id" not in event_dict


def test_inject_trace_context_swallows_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    import opentelemetry.trace as otel_trace

    def _boom() -> None:
        raise RuntimeError("no tracer")

    monkeypatch.setattr(otel_trace, "get_current_span", _boom)
    event_dict = _inject_trace_context(None, "info", {})
    assert event_dict == {}


def test_configure_logging_debug_sets_debug_level() -> None:
    settings = get_settings().model_copy(update={"debug": True})
    configure_logging(settings)
    root_logger = logging.getLogger()
    assert root_logger.level == logging.DEBUG
    assert len(root_logger.handlers) == 1


def test_configure_logging_non_debug_sets_info_level() -> None:
    settings = get_settings().model_copy(update={"debug": False})
    configure_logging(settings)
    root_logger = logging.getLogger()
    assert root_logger.level == logging.INFO
