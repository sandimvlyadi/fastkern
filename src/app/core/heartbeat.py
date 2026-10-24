"""Ops heartbeat: proves beat -> broker -> worker works, and probes ClamAV.

Beat schedules `ops.heartbeat`. A worker runs it and writes short-lived status keys to Redis.
The API only reads those keys, for `/readyz` and the `ops_check_up` metric. The request path never
pings ClamAV itself, so a slow or dead scanner cannot slow down or fail a readiness probe.
"""

import asyncio
import math
from functools import lru_cache

import redis
from prometheus_client import Gauge

from app.core.celery_app import celery_app
from app.core.config import Settings, get_settings
from app.core.di import get_malware_scanner
from app.core.logging import get_logger
from app.modules.files.domain.ports import ScannerUnavailableError

logger = get_logger(__name__)

BEAT_STATUS_KEY = "ops:status:beat"
CLAMAV_STATUS_KEY = "ops:status:clamav"
# Three missed beats before a key expires, so one slow run does not flap the status.
TTL_INTERVALS = 3


@lru_cache(maxsize=1)
def _sync_redis() -> redis.Redis:  # type: ignore[type-arg]
    settings = get_settings()
    return redis.Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_timeout=settings.redis_socket_timeout_seconds,
        socket_connect_timeout=settings.redis_socket_timeout_seconds,
    )


def _ttl_seconds(settings: Settings) -> int:
    return int(settings.ops_heartbeat_interval_seconds * TTL_INTERVALS)


async def _clamav_healthy(settings: Settings) -> bool:
    try:
        await get_malware_scanner().self_test()
    except ScannerUnavailableError:
        logger.warning(
            "clamav_self_test_failed", host=settings.clamav_host, port=settings.clamav_port
        )
        return False
    return True


@celery_app.task(name="ops.heartbeat")  # type: ignore[untyped-decorator]
def heartbeat_task() -> dict[str, str]:
    settings = get_settings()
    ttl = _ttl_seconds(settings)
    client = _sync_redis()
    # Writing the key proves beat scheduled this run, the broker delivered it and a worker ran it.
    client.set(BEAT_STATUS_KEY, "ok", ex=ttl)
    if settings.malware_scan_backend != "clamav":
        client.delete(CLAMAV_STATUS_KEY)
        return {"beat": "ok"}
    clamav = "ok" if asyncio.run(_clamav_healthy(settings)) else "error"
    client.set(CLAMAV_STATUS_KEY, clamav, ex=ttl)
    return {"beat": "ok", "clamav": clamav}


def _key_is_ok(key: str) -> float:
    try:
        return 1.0 if _sync_redis().get(key) == "ok" else 0.0
    except redis.RedisError:
        return 0.0


def _clamav_up() -> float:
    # NaN, not 0, when scanning is off: a 0 would page every deployment that does not run ClamAV.
    if get_settings().malware_scan_backend != "clamav":
        return math.nan
    return _key_is_ok(CLAMAV_STATUS_KEY)


OPS_CHECK_UP = Gauge(
    "ops_check_up",
    "1 while the last heartbeat is recent (check=beat) or clamd passed its self-test scan "
    "(check=clamav). "
    "clamav is NaN when MALWARE_SCAN_BACKEND is none. Computed at scrape time.",
    ["check"],
)
OPS_CHECK_UP.labels("beat").set_function(lambda: _key_is_ok(BEAT_STATUS_KEY))
OPS_CHECK_UP.labels("clamav").set_function(_clamav_up)
