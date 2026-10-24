from types import SimpleNamespace
from typing import Any

import pytest

from app.core import health


async def test_check_database_ok() -> None:
    assert await health.check_database() == "ok"


async def test_check_database_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _broken_session_factory() -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(health, "get_session_factory", lambda: _broken_session_factory)
    assert await health.check_database() == "error"


class _FakePing:
    def __init__(self, *, should_fail: bool) -> None:
        self._should_fail = should_fail

    async def ping(self) -> bool:
        if self._should_fail:
            raise ConnectionError("redis down")
        return True


async def test_check_redis_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, "get_redis", lambda: _FakePing(should_fail=False))
    assert await health.check_redis() == "ok"


async def test_check_redis_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, "get_redis", lambda: _FakePing(should_fail=True))
    assert await health.check_redis() == "error"


class _FakeConnection:
    def __init__(self, *, should_fail: bool) -> None:
        self._should_fail = should_fail
        self.closed = False

    def ensure_connection(self, **kwargs: Any) -> None:
        if self._should_fail:
            raise ConnectionError("broker down")

    def close(self) -> None:
        self.closed = True


async def test_check_celery_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health.celery_app, "connection", lambda: _FakeConnection(should_fail=False))
    assert await health.check_celery() == "ok"


async def test_check_celery_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health.celery_app, "connection", lambda: _FakeConnection(should_fail=True))
    assert await health.check_celery() == "error"


class _FakeGet:
    def __init__(self, value: str | None, *, should_fail: bool = False) -> None:
        self._value = value
        self._should_fail = should_fail
        self.keys: list[str] = []

    async def get(self, key: str) -> str | None:
        self.keys.append(key)
        if self._should_fail:
            raise ConnectionError("redis down")
        return self._value


async def test_check_beat_ok_when_heartbeat_key_is_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeGet("ok")
    monkeypatch.setattr(health, "get_redis", lambda: fake)
    assert await health.check_beat() == "ok"
    assert fake.keys == [health.BEAT_STATUS_KEY]


async def test_check_beat_error_when_key_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, "get_redis", lambda: _FakeGet(None))
    assert await health.check_beat() == "error"


async def test_check_beat_error_when_redis_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, "get_redis", lambda: _FakeGet("ok", should_fail=True))
    assert await health.check_beat() == "error"


async def test_check_clamav_reads_its_own_key(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeGet("error")
    monkeypatch.setattr(health, "get_redis", lambda: fake)
    assert await health.check_clamav() == "error"
    assert fake.keys == [health.CLAMAV_STATUS_KEY]


def _ok_probes(monkeypatch: pytest.MonkeyPatch, *, backend: str = "none") -> None:
    async def _ok() -> str:
        return "ok"

    monkeypatch.setattr(health, "check_database", _ok)
    monkeypatch.setattr(health, "check_redis", _ok)
    monkeypatch.setattr(health, "check_celery", _ok)
    monkeypatch.setattr(health, "check_beat", _ok)
    monkeypatch.setattr(health, "check_clamav", _ok)
    settings = SimpleNamespace(malware_scan_backend=backend)
    monkeypatch.setattr(health, "get_settings", lambda: settings)


async def test_get_readiness_ready_when_all_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    _ok_probes(monkeypatch)

    result = await health.get_readiness()
    assert result == {
        "status": "ready",
        "checks": {"database": "ok", "redis": "ok", "celery": "ok", "beat": "ok"},
    }


async def test_get_readiness_not_ready_when_one_gating_check_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ok_probes(monkeypatch)

    async def _error() -> str:
        return "error"

    monkeypatch.setattr(health, "check_redis", _error)

    result = await health.get_readiness()
    assert result["status"] == "not_ready"
    assert result["checks"] == {"database": "ok", "redis": "error", "celery": "ok", "beat": "ok"}


async def test_dead_beat_is_reported_but_does_not_fail_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ok_probes(monkeypatch)

    async def _error() -> str:
        return "error"

    monkeypatch.setattr(health, "check_beat", _error)

    result = await health.get_readiness()
    assert result["status"] == "ready"
    assert result["checks"] == {"database": "ok", "redis": "ok", "celery": "ok", "beat": "error"}


async def test_clamav_is_reported_only_when_it_is_the_scan_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ok_probes(monkeypatch, backend="none")
    assert "clamav" not in (await health.get_readiness())["checks"]  # type: ignore[operator]

    _ok_probes(monkeypatch, backend="clamav")
    assert (await health.get_readiness())["checks"]["clamav"] == "ok"  # type: ignore[index]


async def test_dead_clamav_is_reported_but_does_not_fail_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ok_probes(monkeypatch, backend="clamav")

    async def _error() -> str:
        return "error"

    monkeypatch.setattr(health, "check_clamav", _error)

    result = await health.get_readiness()
    assert result["status"] == "ready"
    assert result["checks"]["clamav"] == "error"  # type: ignore[index]
