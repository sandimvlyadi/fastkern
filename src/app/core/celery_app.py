"""Celery application for background jobs (emails, notifications, exports, ...).

Tasks live under each module's `infrastructure/tasks.py` and are imported
here via `include=` so `celery -A app.core.celery_app worker` discovers them.
"""

from typing import Any

from celery import Celery
from starlette.concurrency import run_in_threadpool

# Importing the composition root registers every ORM table on Base.metadata. A worker imports only
# this module and its tasks, and a foreign key such as tenant_id -> sys_tenants.id can only be
# resolved if its table is registered; otherwise the first insert that needs it fails in the worker.
import app.core.di  # noqa: F401  (registers all ORM models, see the module docstring there)
from app.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    settings.app_name,
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=[
        "app.modules.notifications.infrastructure.tasks",
        "app.modules.audit_log.infrastructure.tasks",
        "app.modules.files.infrastructure.tasks",
        "app.modules.users.infrastructure.tasks",
        "app.modules.api_clients.infrastructure.tasks",
        "app.core.heartbeat",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=300,
    task_soft_time_limit=270,
    worker_max_tasks_per_child=1000,
    broker_connection_retry_on_startup=True,
    # Run by the `beat` compose service. Cleanup is idempotent, so overlap is safe.
    beat_schedule={
        "files-cleanup": {
            "task": "files.cleanup",
            "schedule": settings.files_cleanup_interval_seconds,
        },
        # Feeds /readyz's beat check and ops_check_up. Idempotent: overlap is safe.
        "ops-heartbeat": {
            "task": "ops.heartbeat",
            "schedule": settings.ops_heartbeat_interval_seconds,
        },
        "api-request-logs-purge": {
            "task": "api_clients.purge_request_logs",
            "schedule": settings.api_request_log_purge_interval_seconds,
        },
    },
)


async def dispatch_task(name: str, **kwargs: Any) -> None:
    """Enqueue task `name` from async code. Use this instead of calling `celery_app.send_task`
    from a router or middleware: publishing is blocking network I/O, so it runs in a worker
    thread rather than stalling the event loop (for every request) while the broker is slow or
    unreachable. A broker outage raises `kombu.exceptions.OperationalError`, which
    `app.core.errors` turns into a 503; the request's session then rolls back.

    `ignore_result=True`: nothing in the API reads a task's result, and without it `send_task`
    first subscribes to the result on the Redis result backend. With Redis down, that retries for
    ~19 s and then raises a bare `RuntimeError` (a 500) instead of the broker's OperationalError."""
    await run_in_threadpool(celery_app.send_task, name, kwargs=kwargs, ignore_result=True)
