"""Readiness checks for external dependencies and the ops heartbeat.

`database`, `redis` and `celery` (the broker) gate `/readyz`. `beat` and `clamav` are reported
only: they are read from the heartbeat's Redis keys, never probed on the request path.

Kept separate from `main.py` so each check is directly unit-testable (see
`tests/unit/test_core_health.py`), following the same pattern as
`core/db.py` / `core/cache.py`.
"""

import asyncio
from collections.abc import Awaitable
from typing import Literal

from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from app.core.cache import get_redis
from app.core.celery_app import celery_app
from app.core.config import get_settings
from app.core.db import get_session_factory
from app.core.heartbeat import BEAT_STATUS_KEY, CLAMAV_STATUS_KEY
from app.core.logging import get_logger

logger = get_logger(__name__)

CheckStatus = Literal["ok", "error"]
GATING_CHECKS = ("database", "redis", "celery")


async def check_database() -> CheckStatus:
    async def _ping() -> None:
        session_factory = get_session_factory()
        async with session_factory() as session:
            await session.execute(text("SELECT 1"))

    try:
        await asyncio.wait_for(_ping(), timeout=get_settings().readiness_check_timeout_seconds)
        return "ok"
    except Exception:
        logger.warning("readiness_check_failed", check="database", exc_info=True)
        return "error"


async def check_redis() -> CheckStatus:
    try:
        await asyncio.wait_for(
            get_redis().ping(), timeout=get_settings().readiness_check_timeout_seconds
        )
        return "ok"
    except Exception:
        logger.warning("readiness_check_failed", check="redis", exc_info=True)
        return "error"


async def check_celery() -> CheckStatus:
    settings = get_settings()

    def _ensure_connection() -> None:
        connection = celery_app.connection()
        try:
            connection.ensure_connection(
                max_retries=1, timeout=settings.readiness_check_timeout_seconds
            )
        finally:
            connection.close()

    try:
        await asyncio.wait_for(
            run_in_threadpool(_ensure_connection),
            timeout=settings.readiness_check_timeout_seconds,
        )
        return "ok"
    except Exception:
        logger.warning("readiness_check_failed", check="celery", exc_info=True)
        return "error"


async def _check_ops_status(key: str, check: str) -> CheckStatus:
    try:
        value = await asyncio.wait_for(
            get_redis().get(key), timeout=get_settings().readiness_check_timeout_seconds
        )
    except Exception:
        logger.warning("readiness_check_failed", check=check, exc_info=True)
        return "error"
    return "ok" if value == "ok" else "error"


async def check_beat() -> CheckStatus:
    """The heartbeat ran within its TTL: beat, the broker and a worker all work."""
    return await _check_ops_status(BEAT_STATUS_KEY, "beat")


async def check_clamav() -> CheckStatus:
    """clamd passed its self-test scan in the last heartbeat (clamav backend only)."""
    return await _check_ops_status(CLAMAV_STATUS_KEY, "clamav")


async def get_readiness() -> dict[str, object]:
    probes: dict[str, Awaitable[CheckStatus]] = {
        "database": check_database(),
        "redis": check_redis(),
        "celery": check_celery(),
        "beat": check_beat(),
    }
    if get_settings().malware_scan_backend == "clamav":
        probes["clamav"] = check_clamav()
    results = dict(zip(probes, await asyncio.gather(*probes.values()), strict=True))
    # Only what the API cannot serve without gates /readyz. A dead scheduler or scanner is reported
    # but must not take every healthy API replica out of rotation.
    overall = "ready" if all(results[name] == "ok" for name in GATING_CHECKS) else "not_ready"
    return {"status": overall, "checks": results}
