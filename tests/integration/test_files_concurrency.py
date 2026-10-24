"""Concurrent chunk uploads. Postgres only: needs real concurrent transactions and the unique index
enforced by the server (same reason as tests/integration/test_concurrent_writes.py)."""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.celery_app import celery_app
from app.core.db import get_engine
from app.main import app
from app.modules.files.infrastructure.storage.local_fs import LocalFileSystemStorage
from app.modules.files.presentation.router import get_object_storage
from tests.helpers import auth_header, grant_role, register_and_login

pytestmark = pytest.mark.skipif(
    get_engine().dialect.name != "postgresql",
    reason="needs concurrent Postgres transactions; see docs/testing.md to run the suite there",
)

BASE = "/api/v1"


async def test_identical_concurrent_chunks_store_exactly_one_row(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app.dependency_overrides[get_object_storage] = lambda: LocalFileSystemStorage(tmp_path / "data")
    monkeypatch.setattr(celery_app, "send_task", lambda *a, **k: None)
    user_id, token, _ = await register_and_login(client, tenant_id, "race@example.com")
    await grant_role(
        session_factory,
        tenant_id=tenant_id,
        user_id=user_id,
        role_name="files",
        permissions=["files:write"],
    )
    initiated = await client.post(
        f"{BASE}/files/uploads",
        json={"filename": "race.bin", "size": 8, "chunkSize": 4},
        headers=auth_header(token),
    )
    upload_id = initiated.json()["data"]["uploadId"]

    def put() -> Any:
        return client.put(
            f"{BASE}/files/uploads/{upload_id}/chunks/0",
            content=b"1234",
            headers={**auth_header(token), "Content-Type": "application/octet-stream"},
        )

    responses: list[Response] = await asyncio.gather(*(put() for _ in range(6)))

    assert {r.status_code for r in responses} == {200}, [r.text for r in responses]
    duplicates = [r.json()["data"]["duplicate"] for r in responses]
    assert duplicates.count(False) == 1, "exactly one request wins the unique (upload, index) row"
    status = await client.get(f"{BASE}/files/uploads/{upload_id}", headers=auth_header(token))
    assert status.json()["data"]["receivedChunks"] == [0]
