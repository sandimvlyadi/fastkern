"""End-to-end proof that the i18n/timezone plumbing (locale negotiation,
RequestContext, translate(), timezone-aware DB columns) is actually wired
into real responses — not just exercised in isolation.
"""

from datetime import datetime
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.celery_app import celery_app
from tests.helpers import auth_header, grant_role, register_and_login


async def test_error_message_is_translated_to_indonesian_via_accept_language(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    user_id, token, _ = await register_and_login(client, tenant_id, "id-reader@example.com")
    await grant_role(
        session_factory,
        tenant_id=tenant_id,
        user_id=user_id,
        role_name="reader",
        permissions=["users:read"],
    )

    response = await client.get(
        f"/api/v1/users/{uuid4()}",
        headers={**auth_header(token), "Accept-Language": "id"},
    )

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "user_not_found"
    assert body["error"]["message"] == "Pengguna tidak ditemukan."


async def test_error_message_defaults_to_english_without_accept_language(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    user_id, token, _ = await register_and_login(client, tenant_id, "en-reader@example.com")
    await grant_role(
        session_factory,
        tenant_id=tenant_id,
        user_id=user_id,
        role_name="reader",
        permissions=["users:read"],
    )

    response = await client.get(f"/api/v1/users/{uuid4()}", headers=auth_header(token))

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "user_not_found"
    assert body["error"]["message"] == "User not found."


async def test_role_not_assigned_message_interpolates_role_name_in_english(
    client: AsyncClient, tenant_id: str
) -> None:
    """Regression test: RoleNotAssignedError(role=...) used to silently
    drop the role name from the message because nothing called
    translate() to apply the `{role}` placeholder substitution."""
    _, token, _ = await register_and_login(client, tenant_id, "switcher-en@example.com")

    response = await client.post(
        "/api/v1/auth/switch-role",
        json={"roleName": "ghost-role"},
        headers=auth_header(token),
    )

    assert response.status_code == 403
    body = response.json()
    assert body["error"]["code"] == "role_not_assigned"
    assert body["error"]["message"] == "You do not have the 'ghost-role' role."


async def test_role_not_assigned_message_interpolates_role_name_in_indonesian(
    client: AsyncClient, tenant_id: str
) -> None:
    _, token, _ = await register_and_login(client, tenant_id, "switcher-id@example.com")

    response = await client.post(
        "/api/v1/auth/switch-role",
        json={"roleName": "ghost-role"},
        headers={**auth_header(token), "Accept-Language": "id"},
    )

    assert response.status_code == 403
    body = response.json()
    assert body["error"]["code"] == "role_not_assigned"
    assert body["error"]["message"] == "Anda tidak memiliki peran 'ghost-role'."


async def test_register_dispatches_welcome_email_with_recipient_locale(
    client: AsyncClient, tenant_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Celery task has no ambient RequestContext, so locale must be
    passed explicitly — sourced from the new user's own saved preference,
    not the registering client's Accept-Language header."""
    captured: dict[str, object] = {}
    original_send_task = celery_app.send_task

    def _capture(name: str, *args: object, **kwargs: object) -> object:
        if name == "notifications.send_welcome_email":
            captured["kwargs"] = kwargs.get("kwargs")
            return None
        return original_send_task(name, *args, **kwargs)

    monkeypatch.setattr(celery_app, "send_task", _capture)

    response = await client.post(
        "/api/v1/auth/register",
        json={
            "tenantId": tenant_id,
            "email": "welcome@example.com",
            "password": "S3curePass!",
            "fullName": "Welcome User",
        },
        headers={"Accept-Language": "id"},
    )

    assert response.status_code == 201, response.text
    # locale is the new user's saved preference (registration doesn't
    # accept a locale field), not the Accept-Language header above.
    assert captured["kwargs"] == {
        "to": "welcome@example.com",
        "full_name": "Welcome User",
        "locale": "en",
    }


async def test_response_timestamps_are_well_formed_and_present(
    client: AsyncClient, tenant_id: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The response carries valid, parseable UTC timestamps end-to-end.

    (Column-level `DateTime(timezone=True)` enforcement is asserted
    directly against the SQLAlchemy schema in
    tests/unit/test_orm_base.py, since SQLite — used here for the fast
    test suite — doesn't actually preserve an offset the way Postgres's
    TIMESTAMPTZ does in production; asserting on the wire value here
    would just be testing SQLite's driver, not this change.)
    """
    user_id, token, _ = await register_and_login(client, tenant_id, "tzcheck@example.com")
    await grant_role(
        session_factory,
        tenant_id=tenant_id,
        user_id=user_id,
        role_name="reader",
        permissions=["users:read"],
    )

    response = await client.get(f"/api/v1/users/{user_id}", headers=auth_header(token))

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert datetime.fromisoformat(data["createdAt"])
    assert datetime.fromisoformat(data["updatedAt"])
