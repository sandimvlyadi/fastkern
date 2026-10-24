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
            "master_examples:read",
            "master_examples:write",
            "master_examples:delete",
            "audit_log:read",
        ],
    )
    return user_id, token


async def test_list_requires_permission(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, no_perm_token, _ = await register_and_login(client, tenant_id, "noperm@example.com")
    forbidden = await client.get("/api/v1/master/examples", headers=auth_header(no_perm_token))
    assert forbidden.status_code == 403

    _, admin_token = await _admin(client, tenant_id, session_factory, "admin@example.com")
    allowed = await client.get("/api/v1/master/examples", headers=auth_header(admin_token))
    assert allowed.status_code == 200
    assert isinstance(allowed.json()["data"], list)
    assert allowed.json()["meta"]["pagination"] is not None


async def test_create_success_records_audit(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, admin_token = await _admin(client, tenant_id, session_factory, "admin2@example.com")

    response = await client.post(
        "/api/v1/master/examples",
        json={"name": "Default Currency", "description": "A reference row."},
        headers=auth_header(admin_token),
    )
    assert response.status_code == 201, response.text
    body = response.json()["data"]
    assert body["name"] == "Default Currency"

    entries = await audit_entries(client, admin_token, "master_example.created")
    assert len(entries) == 1


async def test_create_duplicate_name_conflict(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, admin_token = await _admin(client, tenant_id, session_factory, "admin3@example.com")
    payload = {"name": "Duplicate"}
    await client.post("/api/v1/master/examples", json=payload, headers=auth_header(admin_token))

    response = await client.post(
        "/api/v1/master/examples", json=payload, headers=auth_header(admin_token)
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "master_example_already_exists"


async def test_get_by_id(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, admin_token = await _admin(client, tenant_id, session_factory, "admin4@example.com")
    created = await client.post(
        "/api/v1/master/examples", json={"name": "A"}, headers=auth_header(admin_token)
    )
    example_id = created.json()["data"]["id"]

    response = await client.get(
        f"/api/v1/master/examples/{example_id}", headers=auth_header(admin_token)
    )
    assert response.status_code == 200
    assert response.json()["data"]["id"] == example_id


async def test_get_missing_returns_404(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, admin_token = await _admin(client, tenant_id, session_factory, "admin5@example.com")
    response = await client.get(
        f"/api/v1/master/examples/{uuid4()}", headers=auth_header(admin_token)
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "master_example_not_found"


async def test_update_records_audit_and_noop_patch_does_not(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, admin_token = await _admin(client, tenant_id, session_factory, "admin6@example.com")
    created = await client.post(
        "/api/v1/master/examples", json={"name": "A"}, headers=auth_header(admin_token)
    )
    example_id = created.json()["data"]["id"]

    updated = await client.patch(
        f"/api/v1/master/examples/{example_id}",
        json={"name": "A2"},
        headers=auth_header(admin_token),
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["name"] == "A2"

    noop = await client.patch(
        f"/api/v1/master/examples/{example_id}", json={}, headers=auth_header(admin_token)
    )
    assert noop.status_code == 200

    entries = await audit_entries(client, admin_token, "master_example.updated")
    assert len(entries) == 1


async def test_delete_soft_deletes_and_then_404s(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, admin_token = await _admin(client, tenant_id, session_factory, "admin7@example.com")
    created = await client.post(
        "/api/v1/master/examples", json={"name": "A"}, headers=auth_header(admin_token)
    )
    example_id = created.json()["data"]["id"]

    response = await client.delete(
        f"/api/v1/master/examples/{example_id}", headers=auth_header(admin_token)
    )
    assert response.status_code == 204

    after = await client.get(
        f"/api/v1/master/examples/{example_id}", headers=auth_header(admin_token)
    )
    assert after.status_code == 404

    entries = await audit_entries(client, admin_token, "master_example.deleted")
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
        permissions=["master_examples:read"],
    )
    response = await client.post(
        "/api/v1/master/examples", json={"name": "A"}, headers=auth_header(token)
    )
    assert response.status_code == 403


async def test_error_message_is_translated_to_indonesian(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, admin_token = await _admin(client, tenant_id, session_factory, "admin8@example.com")
    response = await client.get(
        f"/api/v1/master/examples/{uuid4()}",
        headers={**auth_header(admin_token), "Accept-Language": "id"},
    )
    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Master example tidak ditemukan."
