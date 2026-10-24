"""Partner API key lifecycle: create/list/rotate/revoke, RBAC, and plaintext-key exposure rules.

Uses `superadmin` (wildcard permissions) for the privileged paths rather than `admin`: which
tenant-level roles hold `api_clients:*` is seed data, not something this suite should assume is
fresh — `superadmin` is guaranteed by `app.modules.roles.domain.baseline` to always have it.
"""

import uuid

from tests.e2e.conftest import Api, Tokens, data, error_code, expect


def test_listing_and_creating_requires_api_clients_permissions(
    api: Api, plain_user: Tokens, superadmin: Tokens, run_id: str
) -> None:
    expect(api.get("/api-clients", plain_user.access), 403)
    expect(
        api.post("/api-clients", plain_user.access, json={"name": f"e2e-{run_id}"}),
        403,
    )
    expect(api.get("/api-clients", superadmin.access), 200)


def test_create_rotate_revoke_lifecycle(api: Api, superadmin: Tokens, run_id: str) -> None:
    created = data(
        expect(
            api.post(
                "/api-clients",
                superadmin.access,
                json={"name": f"e2e-{run_id}", "scopes": ["posts:read"]},
            ),
            201,
        )
    )
    assert created["status"] == "active"
    assert created["scopes"] == ["posts:read"]
    assert created.get("plaintextKey")
    key_id = created["id"]

    listed = data(api.get("/api-clients", superadmin.access, params={"page_size": 200}))
    assert any(k["id"] == key_id for k in listed)
    # The list response never carries the plaintext — only create/rotate do.
    assert all("plaintextKey" not in k for k in listed)

    rotated = data(expect(api.post(f"/api-clients/{key_id}/rotate", superadmin.access), 200))
    assert rotated["id"] == key_id
    assert rotated["plaintextKey"] != created["plaintextKey"]

    revoked = data(expect(api.post(f"/api-clients/{key_id}/revoke", superadmin.access), 200))
    assert revoked["status"] == "revoked"

    # Revoking again is idempotent (the use case re-saves "revoked" rather than conflicting).
    expect(api.post(f"/api-clients/{key_id}/revoke", superadmin.access), 200)


def test_rotating_a_revoked_key_conflicts(api: Api, superadmin: Tokens, run_id: str) -> None:
    created = data(
        expect(api.post("/api-clients", superadmin.access, json={"name": f"e2e-{run_id}-r"}), 201)
    )
    key_id = created["id"]
    expect(api.post(f"/api-clients/{key_id}/revoke", superadmin.access), 200)

    response = api.post(f"/api-clients/{key_id}/rotate", superadmin.access)
    expect(response, 409, "api_key_already_revoked")


def test_unknown_key_id_is_not_found(api: Api, superadmin: Tokens) -> None:
    missing = uuid.uuid4()
    expect(
        api.post(f"/api-clients/{missing}/rotate", superadmin.access), 404, "api_client_not_found"
    )
    expect(
        api.post(f"/api-clients/{missing}/revoke", superadmin.access), 404, "api_client_not_found"
    )


def test_create_validation_rejects_empty_name(api: Api, superadmin: Tokens) -> None:
    response = api.post("/api-clients", superadmin.access, json={"name": ""})
    expect(response, 422, "validation_error")
    assert error_code(response) == "validation_error"


def test_scope_catalog_lists_what_a_key_can_be_given(
    api: Api, plain_user: Tokens, superadmin: Tokens, run_id: str
) -> None:
    expect(api.get("/api-clients/scopes", plain_user.access), 403)
    scopes = data(expect(api.get("/api-clients/scopes", superadmin.access), 200))
    # The superadmin holds `*`, so every scope is grantable by them.
    assert {
        "code": "posts:read",
        "resource": "posts",
        "action": "read",
        "grantable": True,
    } in scopes
    expect(
        api.post(
            "/api-clients",
            superadmin.access,
            json={"name": f"e2e-{run_id}-bad", "scopes": ["orders:read"]},
        ),
        422,
        "unknown_scope",
    )
