"""The partner (API-key) read surface: each resource answers only to its own `<resource>:read`
scope, and only with rows of the key's tenant."""

import io
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.celery_app import celery_app
from app.core.config import get_settings
from app.core.di import get_object_storage
from app.main import app
from app.modules.api_clients.domain.hashing import generate_api_key
from app.modules.api_clients.infrastructure.models import ApiKeyModel
from app.modules.files.application.pipeline import ProcessUploadUseCase
from app.modules.files.infrastructure.factory import (
    build_image_options,
    build_image_processor,
    build_malware_scanner,
)
from app.modules.files.infrastructure.repository import (
    ChunkRepository,
    FileRepository,
    UploadRepository,
)
from app.modules.files.infrastructure.storage.local_fs import LocalFileSystemStorage
from app.modules.files.presentation.router import get_object_storage as get_files_object_storage
from tests.helpers import auth_header, create_tenant, grant_role, register_and_login

BASE = "/api/v1"


async def _api_key(
    session_factory: async_sessionmaker[AsyncSession], tenant_id: str, scopes: list[str]
) -> dict[str, str]:
    """Commits an active key (the HTTP client reads through its own session, so a flushed-only
    row would be invisible to it) and returns its `X-API-Key` header."""
    plaintext, key_prefix, key_hash = generate_api_key()
    async with session_factory() as session:
        session.add(
            ApiKeyModel(
                tenant_id=UUID(tenant_id),
                name="Partner",
                key_prefix=key_prefix,
                key_hash=key_hash,
                scopes=scopes,
            )
        )
        await session.commit()
    return {"X-API-Key": plaintext}


async def _admin(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    permissions: list[str],
) -> tuple[str, str]:
    user_id, token, _ = await register_and_login(
        client, tenant_id, f"admin-{uuid4().hex[:8]}@example.com"
    )
    await grant_role(
        session_factory,
        tenant_id=tenant_id,
        user_id=user_id,
        role_name=f"admin-{uuid4().hex[:8]}",
        permissions=permissions,
    )
    return user_id, token


_SCOPED_PATHS = [
    "/partners/users",
    f"/partners/users/{uuid4()}",
    "/partners/roles",
    f"/partners/roles/users/{uuid4()}",
    "/partners/master/examples",
    f"/partners/master/examples/{uuid4()}",
    "/partners/examples",
    f"/partners/examples/{uuid4()}",
    f"/partners/files/{uuid4()}",
    f"/partners/files/{uuid4()}/content",
]


async def test_each_resource_requires_its_own_scope(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    headers = await _api_key(session_factory, tenant_id, ["posts:read"])
    for path in _SCOPED_PATHS:
        response = await client.get(f"{BASE}{path}", headers=headers)
        assert response.status_code == 403, f"{path}: {response.text}"
        assert response.json()["error"]["code"] == "scope_denied"


async def test_missing_key_is_unauthorized_before_any_scope_check(client: AsyncClient) -> None:
    response = await client.get(f"{BASE}/partners/users")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"


async def test_users_are_read_within_the_key_tenant_only(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    own_id, _, _ = await register_and_login(client, tenant_id, "own@example.com")
    other_tenant = await create_tenant(session_factory)
    other_id, _, _ = await register_and_login(client, other_tenant, "other@example.com")
    headers = await _api_key(session_factory, tenant_id, ["users:read"])

    listed = await client.get(f"{BASE}/partners/users", headers=headers)
    assert listed.status_code == 200, listed.text
    ids = {u["id"] for u in listed.json()["data"]}
    assert own_id in ids and other_id not in ids
    assert listed.json()["meta"]["pagination"]["total"] == len(ids)

    own = await client.get(f"{BASE}/partners/users/{own_id}", headers=headers)
    assert own.status_code == 200
    assert own.json()["data"]["email"] == "own@example.com"
    assert "avatarUrl" in own.json()["data"]

    other = await client.get(f"{BASE}/partners/users/{other_id}", headers=headers)
    assert other.status_code == 404
    assert other.json()["error"]["code"] == "user_not_found"


async def test_roles_and_a_users_role_assignments_are_readable(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    user_id, _, _ = await register_and_login(client, tenant_id, "member@example.com")
    await grant_role(
        session_factory,
        tenant_id=tenant_id,
        user_id=user_id,
        role_name="auditor",
        permissions=["users:read"],
    )
    headers = await _api_key(session_factory, tenant_id, ["roles:read"])

    roles = await client.get(f"{BASE}/partners/roles", headers=headers)
    assert roles.status_code == 200, roles.text
    assert "auditor" in {r["name"] for r in roles.json()["data"]}

    assigned = await client.get(f"{BASE}/partners/roles/users/{user_id}", headers=headers)
    assert assigned.status_code == 200, assigned.text
    assert [r["name"] for r in assigned.json()["data"]] == ["auditor"]
    assert "isDefault" in assigned.json()["data"][0]


async def test_master_examples_and_examples_are_read_within_the_key_tenant_only(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, token = await _admin(
        client,
        tenant_id,
        session_factory,
        ["master_examples:read", "master_examples:write", "examples:read", "examples:write"],
    )
    master = await client.post(
        f"{BASE}/master/examples",
        json={"name": "Default Currency", "description": "A reference row."},
        headers=auth_header(token),
    )
    assert master.status_code == 201, master.text
    master_id = master.json()["data"]["id"]
    example = await client.post(
        f"{BASE}/examples",
        json={"name": "Invoice #1", "masterExampleId": master_id, "items": ["priority"]},
        headers=auth_header(token),
    )
    assert example.status_code == 201, example.text
    example_id = example.json()["data"]["id"]

    headers = await _api_key(session_factory, tenant_id, ["master_examples:read", "examples:read"])
    masters = await client.get(f"{BASE}/partners/master/examples", headers=headers)
    assert [m["id"] for m in masters.json()["data"]] == [master_id]
    one_master = await client.get(f"{BASE}/partners/master/examples/{master_id}", headers=headers)
    assert one_master.json()["data"]["name"] == "Default Currency"

    examples = await client.get(f"{BASE}/partners/examples", headers=headers)
    assert [e["id"] for e in examples.json()["data"]] == [example_id]
    one = await client.get(f"{BASE}/partners/examples/{example_id}", headers=headers)
    assert one.json()["data"]["masterExampleId"] == master_id
    assert [i["label"] for i in one.json()["data"]["items"]] == ["priority"]

    other_tenant = await create_tenant(session_factory)
    other = await _api_key(session_factory, other_tenant, ["master_examples:read", "examples:read"])
    assert (await client.get(f"{BASE}/partners/examples", headers=other)).json()["data"] == []
    hidden_master = await client.get(f"{BASE}/partners/master/examples/{master_id}", headers=other)
    assert hidden_master.status_code == 404
    assert hidden_master.json()["error"]["code"] == "master_example_not_found"
    hidden = await client.get(f"{BASE}/partners/examples/{example_id}", headers=other)
    assert hidden.status_code == 404
    assert hidden.json()["error"]["code"] == "example_not_found"


@pytest.fixture
def storage_root(tmp_path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    root = tmp_path / "data"
    storage = LocalFileSystemStorage(root)
    app.dependency_overrides[get_object_storage] = lambda: storage
    app.dependency_overrides[get_files_object_storage] = lambda: storage
    monkeypatch.setattr(celery_app, "send_task", lambda *_, **__: None)
    return root


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (40, 30), "teal").save(buffer, "PNG")
    return buffer.getvalue()


async def _process(
    session_factory: async_sessionmaker[AsyncSession], root: Any, upload_id: str, file_id: str
) -> None:
    """Runs the worker pipeline directly, with the same adapters the Celery task uses."""
    work = root.parent / "work"
    work.mkdir(parents=True, exist_ok=True)
    settings = get_settings()
    async with session_factory() as db:
        use_case = ProcessUploadUseCase(
            UploadRepository(db),
            ChunkRepository(db),
            FileRepository(db),
            LocalFileSystemStorage(root),
            build_malware_scanner(settings),
            build_image_processor(),
            build_image_options(settings),
            work,
        )
        assert await use_case.execute(UUID(upload_id), UUID(file_id)) == "AVAILABLE"
        await db.commit()


async def test_files_are_readable_tenant_wide_but_not_across_tenants(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    storage_root,  # type: ignore[no-untyped-def]
) -> None:
    _, token = await _admin(client, tenant_id, session_factory, ["files:write"])
    uploaded = await client.post(
        f"{BASE}/files",
        files={"file": ("photo.png", _png(), "image/png")},
        data={"visibility": "PRIVATE"},
        headers=auth_header(token),
    )
    assert uploaded.status_code == 202, uploaded.text
    upload_id, file_id = uploaded.json()["data"]["uploadId"], uploaded.json()["data"]["fileId"]
    headers = await _api_key(session_factory, tenant_id, ["files:read"])

    pending = await client.get(f"{BASE}/partners/files/{file_id}/content", headers=headers)
    assert pending.status_code == 409
    assert pending.json()["error"]["code"] == "file_not_available"

    await _process(session_factory, storage_root, upload_id, file_id)

    meta = await client.get(f"{BASE}/partners/files/{file_id}", headers=headers)
    assert meta.status_code == 200, meta.text
    assert meta.json()["data"]["status"] == "AVAILABLE"
    assert meta.json()["data"]["originalName"] == "photo.png"

    content = await client.get(f"{BASE}/partners/files/{file_id}/content", headers=headers)
    assert content.status_code == 200
    assert content.headers["content-type"] == "image/webp"
    assert content.headers["cache-control"] == "private, no-store"
    assert content.content

    other_tenant = await create_tenant(session_factory)
    other = await _api_key(session_factory, other_tenant, ["files:read"])
    hidden = await client.get(f"{BASE}/partners/files/{file_id}", headers=other)
    assert hidden.status_code == 404
    assert hidden.json()["error"]["code"] == "file_not_found"
