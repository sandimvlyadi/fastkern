"""Celery publishing never runs on the event loop.

`celery_app.send_task` is blocking network I/O. Called from async code it stalls every in-flight
request while the broker is slow or unreachable, so routers and middleware go through
`dispatch_task`, which publishes from a worker thread.
"""

import re
import threading
import time
from pathlib import Path

import pytest
from celery import Celery
from kombu import exceptions as kombu_exc

import app.core.celery_app as celery_app_module
from app.core.celery_app import dispatch_task

_SRC = Path(__file__).resolve().parents[2] / "src" / "app"
_BLOCKING_PUBLISH = re.compile(r"\.send_task\(|\.delay\(|\.apply_async\(")


async def test_dispatch_task_publishes_off_the_event_loop_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, object], threading.Thread]] = []

    def _record(name: str, *, kwargs: dict[str, object], ignore_result: bool) -> None:
        assert ignore_result is True  # see test_broker_outage_fails_fast_with_a_broker_error
        calls.append((name, kwargs, threading.current_thread()))

    monkeypatch.setattr(celery_app_module.celery_app, "send_task", _record)

    await dispatch_task("module.task_name", a=1, b="two")

    assert len(calls) == 1
    name, kwargs, thread = calls[0]
    assert (name, kwargs) == ("module.task_name", {"a": 1, "b": "two"})
    assert thread is not threading.current_thread()


async def test_broker_outage_fails_fast_with_a_broker_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real Celery app, with the same Redis broker *and* Redis result backend as production,
    both unreachable. Subscribing to the result backend (skipped by `ignore_result=True`) would
    retry for ~19 s and raise a bare RuntimeError, which the API answered with a 500; this is how
    the docker e2e Redis-outage test caught it."""
    # Celery prefers these env vars over the URLs passed below, so an exported one (as when the
    # suite runs against a non-default Redis) would point the probe at a live broker.
    monkeypatch.delenv("CELERY_BROKER_URL", raising=False)
    monkeypatch.delenv("CELERY_RESULT_BACKEND", raising=False)
    unreachable = Celery(
        "outage-probe", broker="redis://127.0.0.1:1/1", backend="redis://127.0.0.1:1/2"
    )
    monkeypatch.setattr(celery_app_module, "celery_app", unreachable)

    started = time.perf_counter()
    with pytest.raises(kombu_exc.OperationalError):
        await dispatch_task("module.task_name", a=1)
    assert time.perf_counter() - started < 5


def test_nothing_publishes_a_task_except_dispatch_task() -> None:
    offenders = []
    for path in sorted(_SRC.rglob("*.py")):
        if path.name == "celery_app.py" and path.parent.name == "core":
            continue
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            code = line.split("#", 1)[0]
            if _BLOCKING_PUBLISH.search(code) and not code.lstrip().startswith(("`", '"')):
                offenders.append(f"{path.relative_to(_SRC)}:{number}: {line.strip()}")
    assert not offenders, (
        "Publish tasks with `await dispatch_task(...)` (app.core.celery_app), not a blocking "
        f"send_task/delay/apply_async: {offenders}"
    )
