import pytest
from httpx import AsyncClient

from app import main
from app.core.cache import get_redis
from app.core.heartbeat import BEAT_STATUS_KEY


async def test_readyz_ok_returns_200(client: AsyncClient) -> None:
    # No beat runs in tests, so stand in for one fresh heartbeat.
    await get_redis().set(BEAT_STATUS_KEY, "ok", ex=60)
    response = await client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"] == {"database": "ok", "redis": "ok", "celery": "ok", "beat": "ok"}


async def test_readyz_not_ready_returns_503(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _not_ready() -> dict[str, object]:
        return {
            "status": "not_ready",
            "checks": {"database": "ok", "redis": "error", "celery": "ok"},
        }

    monkeypatch.setattr(main, "get_readiness", _not_ready)

    response = await client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"


async def test_healthz_returns_200(client: AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
