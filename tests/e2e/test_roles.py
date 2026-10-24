"""Roles: RBAC, creation/escalation guards, permission format, system-role immutability,
assignment lifecycle, instant effect of grants, and a non-stale `activeRole`."""

import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import pytest

from tests.e2e.conftest import Account, Api, Tokens, data, error_code, expect


@contextmanager
def _role(api: Api, token: str, run_id: str, permissions: list[str]) -> Iterator[dict[str, object]]:
    """A custom role that is removed again afterwards (best effort)."""
    name = f"e2e-{run_id}-{uuid.uuid4().hex[:6]}"
    role = data(
        expect(api.post("/roles", token, json={"name": name, "permissions": permissions}), 201)
    )
    try:
        yield role
    finally:
        api.delete(f"/roles/{role['id']}", token)


def test_listing_roles_requires_roles_read(api: Api, plain_user: Tokens, admin: Tokens) -> None:
    expect(api.get("/roles", plain_user.access), 403)
    response = expect(api.get("/roles", admin.access), 200)
    names = {role["name"] for role in data(response)}
    assert {"developer", "superadmin", "admin", "user"} <= names
    assert {"permissions", "isSystem"} <= set(data(response)[0])


def test_seeded_roles_hold_the_intended_permissions(api: Api, admin: Tokens) -> None:
    by_name = {
        r["name"]: r for r in data(api.get("/roles", admin.access, params={"page_size": 200}))
    }

    wildcard_holders = {n for n, r in by_name.items() if "*" in r["permissions"]}
    assert wildcard_holders == {"developer", "superadmin"}
    assert not any(p.startswith("tenants:") for p in by_name["admin"]["permissions"])
    assert by_name["user"]["permissions"] == []
    assert all(by_name[n]["isSystem"] for n in ("developer", "superadmin", "admin", "user"))


def test_create_role_happy_path_and_duplicates(api: Api, admin: Tokens, run_id: str) -> None:
    with _role(api, admin.access, run_id, ["users:read"]) as role:
        assert role["isSystem"] is False
        duplicate = api.post("/roles", admin.access, json={"name": role["name"], "permissions": []})
        expect(duplicate, 409, "role_already_exists")


def test_admin_cannot_create_a_role_beyond_its_own_permissions(
    api: Api, admin: Tokens, plain_user: Tokens, run_id: str
) -> None:
    expect(
        api.post("/roles", admin.access, json={"name": f"esc-{run_id}", "permissions": ["*"]}),
        403,
        "permission_escalation",
    )
    expect(
        api.post(
            "/roles",
            admin.access,
            json={"name": f"esc2-{run_id}", "permissions": ["tenants:manage"]},
        ),
        403,
        "permission_escalation",
    )
    expect(
        api.post("/roles", plain_user.access, json={"name": f"x-{run_id}", "permissions": []}), 403
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"name": ""},
        {"name": "n" * 101},
        {"name": "ok", "permissions": ["not a perm!!"]},
        {"name": "ok", "permissions": ["::"]},
        {"name": "ok", "permissions": ["Users:Read"]},
        {"name": "ok", "permissions": ["users:*"]},
        {"name": "ok", "permissions": [f"res{i}:read" for i in range(101)]},
    ],
)
def test_create_role_validation(api: Api, admin: Tokens, payload: dict[str, object]) -> None:
    expect(api.post("/roles", admin.access, json=payload), 422, "validation_error")


def test_create_role_rejects_a_permission_outside_the_catalog(api: Api, superadmin: Tokens) -> None:
    payload = {"name": "ok", "permissions": ["users:read", "user:read"]}
    expect(api.post("/roles", superadmin.access, json=payload), 422, "unknown_permission")


def test_permission_catalog_is_filtered_and_flagged_per_caller(
    api: Api, plain_user: Tokens, admin: Tokens, superadmin: Tokens
) -> None:
    expect(api.get("/roles/permissions", plain_user.access), 403)
    everything = data(
        expect(api.get("/roles/permissions", superadmin.access, params={"page_size": 200}), 200)
    )
    assert {"*", "tenants:manage", "users:read"} <= {p["code"] for p in everything}
    assert all(p["grantable"] for p in everything)
    tenant_view = data(
        expect(api.get("/roles/permissions", admin.access, params={"page_size": 200}), 200)
    )
    assert not any(p["platformOnly"] for p in tenant_view)


def test_permission_lists_are_deduplicated_and_optional(
    api: Api, admin: Tokens, run_id: str
) -> None:
    # Only permissions the admin itself holds may be granted (`roles:*` would be an escalation).
    with _role(api, admin.access, run_id, ["users:read", "users:read", "roles:read"]) as role:
        assert role["permissions"] == ["users:read", "roles:read"]
    bare = expect(api.post("/roles", admin.access, json={"name": f"bare-{run_id}"}), 201)
    api.delete(f"/roles/{data(bare)['id']}", admin.access)


def test_update_role_rules(
    api: Api, admin: Tokens, superadmin: Tokens, roles: dict[str, str], run_id: str
) -> None:
    with _role(api, admin.access, run_id, ["users:read"]) as role:
        path = f"/roles/{role['id']}"
        updated = expect(
            api.patch(
                path,
                admin.access,
                json={"description": "Updated", "permissions": ["users:read", "roles:read"]},
            ),
            200,
        )
        assert data(updated)["description"] == "Updated"
        expect(
            api.patch(path, admin.access, json={"permissions": ["*"]}), 403, "permission_escalation"
        )
        expect(api.patch(path, admin.access, json={"name": "admin"}), 409, "role_already_exists")
        expect(api.patch(path, admin.access, json={"permissions": ["!!"]}), 422, "validation_error")
        expect(api.patch(path, admin.access, json={}), 200)
    expect(api.patch(f"/roles/{uuid.uuid4()}", admin.access, json={"description": "x"}), 404)
    # system roles are immutable, even for a superadmin
    expect(
        api.patch(f"/roles/{roles['admin']}", superadmin.access, json={"description": "x"}),
        403,
        "system_role_immutable",
    )


def test_system_roles_cannot_be_deleted(
    api: Api, superadmin: Tokens, roles: dict[str, str]
) -> None:
    expect(api.delete(f"/roles/{roles['admin']}", superadmin.access), 403, "system_role_immutable")


def test_assignment_lifecycle_is_idempotent(
    api: Api, admin: Tokens, plain_user: Tokens, make_user: Callable[..., Account], run_id: str
) -> None:
    account = make_user()
    with _role(api, admin.access, run_id, ["users:read"]) as role:
        assign = f"/roles/users/{account.id}"
        assert [r["name"] for r in data(api.get(assign, admin.access))] == ["user"]

        expect(api.post(assign, admin.access, json={"roleId": role["id"]}), 204)
        expect(api.post(assign, admin.access, json={"roleId": role["id"]}), 204)  # idempotent
        assert len(data(api.get(assign, admin.access))) == 2  # no duplicate grant

        expect(api.post(assign, plain_user.access, json={"roleId": role["id"]}), 403)
        expect(api.post(assign, admin.access, json={"roleId": str(uuid.uuid4())}), 404)
        expect(
            api.post(f"/roles/users/{uuid.uuid4()}", admin.access, json={"roleId": role["id"]}), 404
        )
        expect(api.post(assign, admin.access, json={}), 422, "validation_error")

        expect(api.delete(f"{assign}/{role['id']}", plain_user.access), 403)
        expect(api.delete(f"{assign}/{role['id']}", admin.access), 204)
        expect(api.delete(f"{assign}/{role['id']}", admin.access), 204)  # idempotent


def test_admin_cannot_assign_a_role_it_does_not_hold_itself(
    api: Api, admin: Tokens, make_user: Callable[..., Account], roles: dict[str, str]
) -> None:
    account = make_user()
    response = api.post(
        f"/roles/users/{account.id}", admin.access, json={"roleId": roles["superadmin"]}
    )
    expect(response, 403, "permission_escalation")


def test_a_grant_takes_effect_immediately_and_so_does_its_revocation(
    api: Api, admin: Tokens, make_user: Callable[..., Account], run_id: str
) -> None:
    account = make_user()
    expect(api.get("/roles", account.tokens.access), 403)
    with _role(api, admin.access, run_id, ["roles:read"]) as role:
        assign = f"/roles/users/{account.id}"
        api.post(assign, admin.access, json={"roleId": role["id"]})
        expect(api.get("/roles", account.tokens.access), 200)

        api.delete(f"{assign}/{role['id']}", admin.access)
        expect(api.get("/roles", account.tokens.access), 403)


def test_active_role_is_never_stale_after_a_role_is_revoked(
    api: Api, admin: Tokens, make_user: Callable[..., Account], run_id: str
) -> None:
    account = make_user()
    with _role(api, admin.access, run_id, []) as role:
        assign = f"/roles/users/{account.id}"
        api.post(assign, admin.access, json={"roleId": role["id"]})
        switched = data(
            expect(
                api.post(
                    "/auth/switch-role", account.tokens.access, json={"roleName": role["name"]}
                ),
                200,
            )
        )
        me = data(api.get("/auth/me", switched["accessToken"]))
        assert me["activeRole"] == role["name"]

        api.delete(f"{assign}/{role['id']}", admin.access)

        stale = data(expect(api.get("/auth/me", switched["accessToken"]), 200))
        assert stale["activeRole"] is None  # not advertising a role they no longer hold
        refreshed = data(
            expect(api.post("/auth/refresh", json={"refreshToken": switched["refreshToken"]}), 200)
        )
        after = data(api.get("/auth/me", refreshed["accessToken"]))
        assert after["activeRole"] == "user"  # fell back to a role they still hold


def test_deleting_an_assigned_role_removes_it_and_its_effect(
    api: Api, admin: Tokens, plain_user: Tokens, make_user: Callable[..., Account], run_id: str
) -> None:
    account = make_user()
    role = data(
        expect(
            api.post(
                "/roles",
                admin.access,
                json={"name": f"del-{run_id}", "permissions": ["roles:read"]},
            ),
            201,
        )
    )
    api.post(f"/roles/users/{account.id}", admin.access, json={"roleId": role["id"]})
    expect(api.get("/roles", account.tokens.access), 200)
    expect(api.delete(f"/roles/{role['id']}", plain_user.access), 403)

    expect(api.delete(f"/roles/{role['id']}", admin.access), 204)

    assert [r["name"] for r in data(api.get(f"/roles/users/{account.id}", admin.access))] == [
        "user"
    ]
    expect(api.get("/roles", account.tokens.access), 403)  # its permission no longer applies
    expect(api.delete(f"/roles/{role['id']}", admin.access), 404)
    expect(api.post(f"/roles/users/{account.id}", admin.access, json={"roleId": role["id"]}), 404)
    # the freed name can be reused
    again = api.post("/roles", admin.access, json={"name": f"del-{run_id}", "permissions": []})
    assert again.status_code == 201, error_code(again) if again.status_code != 201 else ""
    api.delete(f"/roles/{data(again)['id']}", admin.access)
