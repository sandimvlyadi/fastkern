import math
from types import SimpleNamespace
from typing import Any

import pytest

from app.core import heartbeat
from app.modules.files.domain.ports import ScannerUnavailableError


class _FakeRedis:
    def __init__(self, values: dict[str, str] | None = None, *, should_fail: bool = False) -> None:
        self.values = dict(values or {})
        self.sets: list[tuple[str, str, int | None]] = []
        self.deleted: list[str] = []
        self._should_fail = should_fail

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.sets.append((key, value, ex))
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.values.pop(key, None)

    def get(self, key: str) -> str | None:
        if self._should_fail:
            raise heartbeat.redis.ConnectionError("redis down")
        return self.values.get(key)


def _settings(backend: str) -> SimpleNamespace:
    return SimpleNamespace(
        ops_heartbeat_interval_seconds=60.0,
        malware_scan_backend=backend,
        clamav_host="clamav",
        clamav_port=3310,
        clamav_timeout_seconds=5.0,
    )


class _FakeScanner:
    outcome: Exception | None = None

    async def self_test(self) -> None:
        if _FakeScanner.outcome is not None:
            raise _FakeScanner.outcome


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    client = _FakeRedis()
    monkeypatch.setattr(heartbeat, "_sync_redis", lambda: client)
    return client


def test_heartbeat_without_clamav_writes_beat_with_three_interval_ttl(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis
) -> None:
    monkeypatch.setattr(heartbeat, "get_settings", lambda: _settings("none"))
    fake_redis.values[heartbeat.CLAMAV_STATUS_KEY] = "error"  # left over from an earlier config

    result: Any = heartbeat.heartbeat_task()

    assert result == {"beat": "ok"}
    assert fake_redis.sets == [(heartbeat.BEAT_STATUS_KEY, "ok", 180)]
    assert fake_redis.deleted == [heartbeat.CLAMAV_STATUS_KEY]


def test_heartbeat_records_clamav_ok_when_clamd_answers(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis
) -> None:
    monkeypatch.setattr(heartbeat, "get_settings", lambda: _settings("clamav"))
    monkeypatch.setattr(heartbeat, "get_malware_scanner", _FakeScanner)
    _FakeScanner.outcome = None

    result: Any = heartbeat.heartbeat_task()

    assert result == {"beat": "ok", "clamav": "ok"}
    assert (heartbeat.CLAMAV_STATUS_KEY, "ok", 180) in fake_redis.sets


def test_heartbeat_records_clamav_error_without_failing_the_task(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis
) -> None:
    monkeypatch.setattr(heartbeat, "get_settings", lambda: _settings("clamav"))
    monkeypatch.setattr(heartbeat, "get_malware_scanner", _FakeScanner)
    _FakeScanner.outcome = ScannerUnavailableError("connection refused")

    result: Any = heartbeat.heartbeat_task()

    assert result == {"beat": "ok", "clamav": "error"}
    assert (heartbeat.CLAMAV_STATUS_KEY, "error", 180) in fake_redis.sets
    assert (heartbeat.BEAT_STATUS_KEY, "ok", 180) in fake_redis.sets


def test_beat_gauge_is_one_only_for_a_fresh_ok_key(fake_redis: _FakeRedis) -> None:
    fake_redis.values[heartbeat.BEAT_STATUS_KEY] = "ok"
    assert heartbeat._key_is_ok(heartbeat.BEAT_STATUS_KEY) == 1.0

    fake_redis.values.clear()
    assert heartbeat._key_is_ok(heartbeat.BEAT_STATUS_KEY) == 0.0


def test_beat_gauge_is_zero_when_redis_cannot_be_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(heartbeat, "_sync_redis", lambda: _FakeRedis(should_fail=True))
    assert heartbeat._key_is_ok(heartbeat.BEAT_STATUS_KEY) == 0.0


def test_clamav_gauge_is_nan_when_scanning_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(heartbeat, "get_settings", lambda: _settings("none"))
    assert math.isnan(heartbeat._clamav_up())


def test_clamav_gauge_reads_the_status_key_when_scanning_is_on(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis
) -> None:
    monkeypatch.setattr(heartbeat, "get_settings", lambda: _settings("clamav"))
    fake_redis.values[heartbeat.CLAMAV_STATUS_KEY] = "ok"

    assert heartbeat._clamav_up() == 1.0
