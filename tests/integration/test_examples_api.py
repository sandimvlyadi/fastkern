from uuid import uuid4

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.helpers import audit_entries, auth_header, grant_role, register_and_login


async def _admin(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    email: str,
) -> tuple[str, str]:
    user_id, token, _ = await register_and_login(client, tenant_id, email)
    await grant_role(
        session_factory,
        tenant_id=tenant_id,
        user_id=user_id,
        role_name="admin",
        permissions=[
            "examples:read",
            "examples:write",
            "examples:delete",
            "master_examples:read",
            "master_examples:write",
            "audit_log:read",
        ],
    )
    return user_id, token


async def test_list_requires_permission(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, no_perm_token, _ = await register_and_login(client, tenant_id, "noperm@example.com")
    forbidden = await client.get("/api/v1/examples", headers=auth_header(no_perm_token))
    assert forbidden.status_code == 403

    _, admin_token = await _admin(client, tenant_id, session_factory, "admin@example.com")
    allowed = await client.get("/api/v1/examples", headers=auth_header(admin_token))
    assert allowed.status_code == 200


async def test_create_without_master_example_id_succeeds(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, admin_token = await _admin(client, tenant_id, session_factory, "admin2@example.com")
    response = await client.post(
        "/api/v1/examples",
        json={"name": "Invoice #1", "items": ["priority", "reviewed"]},
        headers=auth_header(admin_token),
    )
    assert response.status_code == 201, response.text
    body = response.json()["data"]
    assert body["masterExampleId"] is None
    assert {i["label"] for i in body["items"]} == {"priority", "reviewed"}

    entries = await audit_entries(client, admin_token, "example.created")
    assert len(entries) == 1


async def test_create_with_unknown_master_example_id_returns_404(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, admin_token = await _admin(client, tenant_id, session_factory, "admin3@example.com")
    response = await client.post(
        "/api/v1/examples",
        json={"name": "Invoice #1", "masterExampleId": str(uuid4())},
        headers=auth_header(admin_token),
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "master_example_not_found"


async def test_create_with_valid_master_example_id(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, admin_token = await _admin(client, tenant_id, session_factory, "admin4@example.com")
    master = await client.post(
        "/api/v1/master/examples",
        json={"name": "Default Currency"},
        headers=auth_header(admin_token),
    )
    master_id = master.json()["data"]["id"]

    response = await client.post(
        "/api/v1/examples",
        json={"name": "Invoice #1", "masterExampleId": master_id},
        headers=auth_header(admin_token),
    )
    assert response.status_code == 201, response.text
    assert response.json()["data"]["masterExampleId"] == master_id


async def test_update_items_replaces_wholesale(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, admin_token = await _admin(client, tenant_id, session_factory, "admin5@example.com")
    created = await client.post(
        "/api/v1/examples",
        json={"name": "Invoice #1", "items": ["old1", "old2"]},
        headers=auth_header(admin_token),
    )
    example_id = created.json()["data"]["id"]

    updated = await client.patch(
        f"/api/v1/examples/{example_id}",
        json={"items": ["new1"]},
        headers=auth_header(admin_token),
    )
    assert updated.status_code == 200
    assert [i["label"] for i in updated.json()["data"]["items"]] == ["new1"]

    fetched = await client.get(f"/api/v1/examples/{example_id}", headers=auth_header(admin_token))
    assert [i["label"] for i in fetched.json()["data"]["items"]] == ["new1"]


async def test_update_omitting_master_example_id_leaves_it_unchanged(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, admin_token = await _admin(client, tenant_id, session_factory, "admin6@example.com")
    master = await client.post(
        "/api/v1/master/examples",
        json={"name": "Default Currency"},
        headers=auth_header(admin_token),
    )
    master_id = master.json()["data"]["id"]
    created = await client.post(
        "/api/v1/examples",
        json={"name": "Invoice #1", "masterExampleId": master_id},
        headers=auth_header(admin_token),
    )
    example_id = created.json()["data"]["id"]

    updated = await client.patch(
        f"/api/v1/examples/{example_id}",
        json={"name": "Invoice #1 (revised)"},
        headers=auth_header(admin_token),
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["masterExampleId"] == master_id


async def test_update_explicit_null_master_example_id_clears_it(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, admin_token = await _admin(client, tenant_id, session_factory, "admin7@example.com")
    master = await client.post(
        "/api/v1/master/examples",
        json={"name": "Default Currency"},
        headers=auth_header(admin_token),
    )
    master_id = master.json()["data"]["id"]
    created = await client.post(
        "/api/v1/examples",
        json={"name": "Invoice #1", "masterExampleId": master_id},
        headers=auth_header(admin_token),
    )
    example_id = created.json()["data"]["id"]

    updated = await client.patch(
        f"/api/v1/examples/{example_id}",
        json={"masterExampleId": None},
        headers=auth_header(admin_token),
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["masterExampleId"] is None


async def test_delete_soft_deletes_and_records_audit(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, admin_token = await _admin(client, tenant_id, session_factory, "admin8@example.com")
    created = await client.post(
        "/api/v1/examples", json={"name": "Invoice #1"}, headers=auth_header(admin_token)
    )
    example_id = created.json()["data"]["id"]

    response = await client.delete(
        f"/api/v1/examples/{example_id}", headers=auth_header(admin_token)
    )
    assert response.status_code == 204

    after = await client.get(f"/api/v1/examples/{example_id}", headers=auth_header(admin_token))
    assert after.status_code == 404

    entries = await audit_entries(client, admin_token, "example.deleted")
    assert len(entries) == 1


async def test_write_requires_write_permission_not_just_read(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    user_id, token, _ = await register_and_login(client, tenant_id, "reader@example.com")
    await grant_role(
        session_factory,
        tenant_id=tenant_id,
        user_id=user_id,
        role_name="reader",
        permissions=["examples:read"],
    )
    response = await client.post(
        "/api/v1/examples", json={"name": "Invoice #1"}, headers=auth_header(token)
    )
    assert response.status_code == 403
