from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from httpx import AsyncClient
from kombu import exceptions as kombu_exc
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.core.celery_app as celery_app_module
from app.core.di import get_posts_api_port
from app.modules.api_clients.infrastructure.models import ApiKeyModel
from app.modules.posts.domain.entities import Post
from tests.fakes import InMemoryPostsApiAdapter
from tests.helpers import audit_entries, auth_header, grant_role, register_and_login


def _override_posts_api() -> None:
    from app.main import app

    app.dependency_overrides[get_posts_api_port] = lambda: InMemoryPostsApiAdapter(
        seed=[Post(id=1, user_id=1, title="t1", body="b")]
    )


async def _admin(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> str:
    user_id, token, _ = await register_and_login(client, tenant_id, "admin@example.com")
    await grant_role(
        session_factory,
        tenant_id=tenant_id,
        user_id=user_id,
        role_name="admin",
        # posts:read too: a key may only be given scopes its creator holds as permissions.
        permissions=["api_clients:read", "api_clients:write", "audit_log:read", "posts:read"],
    )
    return token


def _api_key_header(plaintext: str) -> dict[str, str]:
    return {"X-API-Key": plaintext}


async def test_create_key_requires_permission(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, no_perm_token, _ = await register_and_login(client, tenant_id, "noperm@example.com")
    forbidden = await client.post(
        "/api/v1/api-clients",
        json={"name": "Acme", "scopes": ["posts:read"]},
        headers=auth_header(no_perm_token),
    )
    assert forbidden.status_code == 403


async def test_create_key_returns_plaintext_once_then_can_authenticate(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _override_posts_api()
    admin_token = await _admin(client, tenant_id, session_factory)

    created = await client.post(
        "/api/v1/api-clients",
        json={"name": "Acme Corp integration", "scopes": ["posts:read"]},
        headers=auth_header(admin_token),
    )
    assert created.status_code == 201, created.text
    body = created.json()["data"]
    plaintext = body["plaintextKey"]
    assert plaintext

    partner_response = await client.get(
        "/api/v1/partners/posts", headers=_api_key_header(plaintext)
    )
    assert partner_response.status_code == 200, partner_response.text

    # The audit log records the creation as actor_type "api_client" would never be — the
    # admin's human user did this, so it stays "user".
    entries = await audit_entries(client, admin_token, "api_client.created")
    assert entries
    assert entries[0]["actorType"] == "user"


async def test_partner_call_without_key_is_rejected(client: AsyncClient) -> None:
    _override_posts_api()
    response = await client.get("/api/v1/partners/posts")
    assert response.status_code == 401


async def test_partner_call_with_insufficient_scope_is_forbidden(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _override_posts_api()
    admin_token = await _admin(client, tenant_id, session_factory)
    created = await client.post(
        "/api/v1/api-clients",
        json={"name": "Read-nothing key", "scopes": []},
        headers=auth_header(admin_token),
    )
    plaintext = created.json()["data"]["plaintextKey"]

    response = await client.get("/api/v1/partners/posts", headers=_api_key_header(plaintext))
    assert response.status_code == 403


async def test_partner_can_fetch_a_single_post_by_id(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _override_posts_api()
    admin_token = await _admin(client, tenant_id, session_factory)
    created = await client.post(
        "/api/v1/api-clients",
        json={"name": "Acme Corp integration", "scopes": ["posts:read"]},
        headers=auth_header(admin_token),
    )
    plaintext = created.json()["data"]["plaintextKey"]

    response = await client.get("/api/v1/partners/posts/1", headers=_api_key_header(plaintext))
    assert response.status_code == 200
    assert response.json()["data"]["id"] == 1

    missing = await client.get("/api/v1/partners/posts/999", headers=_api_key_header(plaintext))
    assert missing.status_code == 404


async def test_rotate_keeps_old_key_valid_then_revoke_invalidates_both(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _override_posts_api()
    admin_token = await _admin(client, tenant_id, session_factory)
    created = await client.post(
        "/api/v1/api-clients",
        json={"name": "Acme", "scopes": ["posts:read"]},
        headers=auth_header(admin_token),
    )
    key_id = created.json()["data"]["id"]
    old_plaintext = created.json()["data"]["plaintextKey"]

    rotated = await client.post(
        f"/api/v1/api-clients/{key_id}/rotate", headers=auth_header(admin_token)
    )
    assert rotated.status_code == 200, rotated.text
    new_plaintext = rotated.json()["data"]["plaintextKey"]

    # Both still work, inside the rotation overlap window.
    old_call = await client.get("/api/v1/partners/posts", headers=_api_key_header(old_plaintext))
    new_call = await client.get("/api/v1/partners/posts", headers=_api_key_header(new_plaintext))
    assert old_call.status_code == 200
    assert new_call.status_code == 200

    revoked = await client.post(
        f"/api/v1/api-clients/{key_id}/revoke", headers=auth_header(admin_token)
    )
    assert revoked.status_code == 200, revoked.text

    old_call_after_revoke = await client.get(
        "/api/v1/partners/posts", headers=_api_key_header(old_plaintext)
    )
    new_call_after_revoke = await client.get(
        "/api/v1/partners/posts", headers=_api_key_header(new_plaintext)
    )
    assert old_call_after_revoke.status_code == 401
    assert new_call_after_revoke.status_code == 401


async def test_rotating_a_revoked_key_is_rejected(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Regression test: rotating an already-revoked key used to succeed (200) and return a
    plaintext that could never authenticate, with no signal why."""
    _override_posts_api()
    admin_token = await _admin(client, tenant_id, session_factory)
    created = await client.post(
        "/api/v1/api-clients",
        json={"name": "Acme", "scopes": ["posts:read"]},
        headers=auth_header(admin_token),
    )
    key_id = created.json()["data"]["id"]

    revoked = await client.post(
        f"/api/v1/api-clients/{key_id}/revoke", headers=auth_header(admin_token)
    )
    assert revoked.status_code == 200, revoked.text

    rotated = await client.post(
        f"/api/v1/api-clients/{key_id}/rotate", headers=auth_header(admin_token)
    )
    assert rotated.status_code == 409, rotated.text
    assert rotated.json()["error"]["code"] == "api_key_already_revoked"


async def test_partner_call_dispatches_a_request_log_entry(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test: `ApiTrafficLogMiddleware` used to be registered OUTSIDE
    `RequestContextMiddleware`, so by the time its own `finally` ran, the request context had
    already been reset to the default and `api_client_id` was always `None` — the entire
    traffic-log feature silently dispatched nothing. See `main.py`'s middleware-order
    comment."""
    _override_posts_api()
    admin_token = await _admin(client, tenant_id, session_factory)
    created = await client.post(
        "/api/v1/api-clients",
        json={"name": "Acme", "scopes": ["posts:read"]},
        headers=auth_header(admin_token),
    )
    plaintext = created.json()["data"]["plaintextKey"]

    captured: list[tuple[str, dict[str, object]]] = []
    original_send_task = celery_app_module.celery_app.send_task

    def _capture(name: str, *args: object, **kwargs: object) -> object:
        if name == "api_clients.record_request_log":
            captured.append((name, kwargs.get("kwargs", {})))
            return None
        return original_send_task(name, *args, **kwargs)

    monkeypatch.setattr(celery_app_module.celery_app, "send_task", _capture)

    response = await client.get("/api/v1/partners/posts", headers=_api_key_header(plaintext))
    assert response.status_code == 200, response.text

    assert captured, "ApiTrafficLogMiddleware should have dispatched a record_request_log task"
    _, task_kwargs = captured[0]
    assert task_kwargs["method"] == "GET"
    assert task_kwargs["path"] == "/api/v1/partners/posts"
    assert task_kwargs["status_code"] == 200


async def test_non_partner_call_does_not_dispatch_a_request_log_entry(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A human (JWT-authenticated) request must never be logged as partner traffic."""
    admin_token = await _admin(client, tenant_id, session_factory)

    captured: list[str] = []
    original_send_task = celery_app_module.celery_app.send_task

    def _capture(name: str, *args: object, **kwargs: object) -> object:
        if name == "api_clients.record_request_log":
            captured.append(name)
            return None
        return original_send_task(name, *args, **kwargs)

    monkeypatch.setattr(celery_app_module.celery_app, "send_task", _capture)

    response = await client.get("/api/v1/api-clients", headers=auth_header(admin_token))
    assert response.status_code == 200, response.text
    assert captured == []


async def test_partner_call_still_succeeds_when_the_request_log_cannot_be_enqueued(
    client: AsyncClient,
    tenant_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The traffic log is best effort: a broker outage costs a log row, not the partner's 200."""
    _override_posts_api()
    admin_token = await _admin(client, tenant_id, session_factory)
    created = await client.post(
        "/api/v1/api-clients",
        json={"name": "Acme", "scopes": ["posts:read"]},
        headers=auth_header(admin_token),
    )
    plaintext = created.json()["data"]["plaintextKey"]

    def _broker_down(*args: object, **kwargs: object) -> None:
        raise kombu_exc.OperationalError("Error 111 connecting to redis:6379. Connection refused.")

    monkeypatch.setattr(celery_app_module.celery_app, "send_task", _broker_down)

    response = await client.get("/api/v1/partners/posts", headers=_api_key_header(plaintext))
    assert response.status_code == 200, response.text


async def _key_manager(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> str:
    """Can manage keys, but holds no read permission on any partner resource."""
    user_id, token, _ = await register_and_login(client, tenant_id, "keymanager@example.com")
    await grant_role(
        session_factory,
        tenant_id=tenant_id,
        user_id=user_id,
        role_name="key-manager",
        permissions=["api_clients:read", "api_clients:write"],
    )
    return token


async def test_creating_a_key_with_scopes_the_caller_lacks_is_forbidden(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Regression test: `api_clients:write` alone used to mint a key that reads every user and
    file in the tenant, which its creator could not read themselves."""
    token = await _key_manager(client, tenant_id, session_factory)

    response = await client.post(
        "/api/v1/api-clients",
        json={"name": "Escalation", "scopes": ["users:read", "files:read"]},
        headers=auth_header(token),
    )

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "scope_escalation"
    listed = await client.get("/api/v1/api-clients", headers=auth_header(token))
    assert listed.json()["data"] == []


async def test_rotating_a_key_with_scopes_the_caller_lacks_is_forbidden(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Rotation hands out a working plaintext, so it must not let a key manager take over a
    broader key someone else created."""
    admin_token = await _admin(client, tenant_id, session_factory)
    created = await client.post(
        "/api/v1/api-clients",
        json={"name": "Acme", "scopes": ["posts:read"]},
        headers=auth_header(admin_token),
    )
    key_id = created.json()["data"]["id"]
    manager_token = await _key_manager(client, tenant_id, session_factory)

    rotated = await client.post(
        f"/api/v1/api-clients/{key_id}/rotate", headers=auth_header(manager_token)
    )

    assert rotated.status_code == 403, rotated.text
    assert rotated.json()["error"]["code"] == "scope_escalation"


async def test_rotating_an_expired_key_is_rejected(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    admin_token = await _admin(client, tenant_id, session_factory)
    created = await client.post(
        "/api/v1/api-clients",
        json={"name": "Acme", "scopes": ["posts:read"]},
        headers=auth_header(admin_token),
    )
    key_id = created.json()["data"]["id"]
    # The API refuses a past expiry, so age the key directly.
    async with session_factory() as session:
        await session.execute(
            update(ApiKeyModel)
            .where(ApiKeyModel.id == UUID(key_id))
            .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
        )
        await session.commit()

    rotated = await client.post(
        f"/api/v1/api-clients/{key_id}/rotate", headers=auth_header(admin_token)
    )

    assert rotated.status_code == 409, rotated.text
    assert rotated.json()["error"]["code"] == "api_key_already_expired"


async def test_creating_a_key_that_is_already_expired_is_422(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    admin_token = await _admin(client, tenant_id, session_factory)

    response = await client.post(
        "/api/v1/api-clients",
        json={
            "name": "Acme",
            "scopes": ["posts:read"],
            "expiresAt": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
        },
        headers=auth_header(admin_token),
    )

    assert response.status_code == 422, response.text


async def test_a_partner_call_does_not_write_the_key_row(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Verifying a key is read-only, so concurrent calls with one key never queue on its row
    lock; `lastUsedAt` is recorded later by the request-log task."""
    _override_posts_api()
    admin_token = await _admin(client, tenant_id, session_factory)
    created = await client.post(
        "/api/v1/api-clients",
        json={"name": "Acme", "scopes": ["posts:read"]},
        headers=auth_header(admin_token),
    )
    plaintext = created.json()["data"]["plaintextKey"]

    call = await client.get("/api/v1/partners/posts", headers=_api_key_header(plaintext))
    assert call.status_code == 200

    listed = await client.get("/api/v1/api-clients", headers=auth_header(admin_token))
    [key] = listed.json()["data"]
    assert key["lastUsedAt"] is None
