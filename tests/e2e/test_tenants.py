"""Tenants: platform-only management (a tenant admin has no access), baseline roles for new
tenants, cross-tenant isolation, and the suspend / activate / delete lifecycle."""

import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import pytest

from tests.e2e.conftest import STRONG_PASSWORD, TENANT_ID, Account, Api, Tokens, data, expect


@contextmanager
def _tenant(api: Api, superadmin: Tokens, run_id: str) -> Iterator[dict[str, str]]:
    """A fresh tenant that is deleted again afterwards (best effort)."""
    slug = f"e2e-{run_id}-{uuid.uuid4().hex[:6]}"
    tenant = data(
        expect(
            api.post("/tenants", superadmin.access, json={"name": "E2E Tenant", "slug": slug}), 201
        )
    )
    try:
        yield tenant
    finally:
        api.delete(f"/tenants/{tenant['id']}", superadmin.access)


def _register_in(api: Api, tenant_id: str, email: str) -> str:
    response = expect(
        api.post(
            "/auth/register",
            json={
                "tenantId": tenant_id,
                "email": email,
                "password": STRONG_PASSWORD,
                "fullName": "Tenant Member",
            },
        ),
        201,
    )
    return str(data(response)["id"])


# --- platform-only --------------------------------------------------------------------------------


def test_a_tenant_admin_cannot_manage_tenants(
    api: Api, admin: Tokens, plain_user: Tokens, superadmin: Tokens, run_id: str
) -> None:
    with _tenant(api, superadmin, run_id) as other:
        for token in (admin.access, plain_user.access):
            expect(api.get("/tenants", token), 403)
            expect(
                api.post("/tenants", token, json={"name": "Rogue", "slug": f"rogue-{run_id}"}), 403
            )
            expect(api.patch(f"/tenants/{other['id']}", token, json={"name": "HACKED"}), 403)
            expect(api.post(f"/tenants/{other['id']}/deactivate", token), 403)
            expect(api.post(f"/tenants/{other['id']}/activate", token), 403)
            expect(api.delete(f"/tenants/{other['id']}", token), 403)
        # ...and the other org's name is untouched.
        listing = data(api.get("/tenants", superadmin.access, params={"page_size": 200}))
        assert next(t for t in listing if t["id"] == other["id"])["name"] == "E2E Tenant"


def test_the_superadmin_manages_tenants(api: Api, superadmin: Tokens) -> None:
    response = expect(api.get("/tenants", superadmin.access), 200)
    assert {"id", "name", "slug", "isActive"} <= set(data(response)[0])


# --- CRUD ------------------------------------------------------------------------------------


def test_create_update_and_validation(api: Api, superadmin: Tokens, run_id: str) -> None:
    with _tenant(api, superadmin, run_id) as tenant:
        assert tenant["isActive"] is True
        path = f"/tenants/{tenant['id']}"

        renamed = expect(
            api.patch(
                path, superadmin.access, json={"name": "Renamed", "slug": f"{tenant['slug']}-2"}
            ),
            200,
        )
        assert data(renamed)["name"] == "Renamed"
        expect(
            api.patch(path, superadmin.access, json={"slug": "default"}),
            409,
            "tenant_slug_already_exists",
        )
        expect(api.patch(f"/tenants/{uuid.uuid4()}", superadmin.access, json={"name": "x"}), 404)
        ignored = expect(api.patch(path, superadmin.access, json={"isActive": False}), 200)
        assert data(ignored)["isActive"] is True  # activation has its own endpoints

        duplicate = api.post(
            "/tenants", superadmin.access, json={"name": "Dup", "slug": data(renamed)["slug"]}
        )
        expect(duplicate, 409, "tenant_slug_already_exists")


@pytest.mark.parametrize("slug", ["Bad Slug", "UPPER", "under_score", "", "a" * 101])
def test_invalid_slugs_are_422(api: Api, superadmin: Tokens, slug: str) -> None:
    expect(
        api.post("/tenants", superadmin.access, json={"name": "X", "slug": slug}),
        422,
        "validation_error",
    )


def test_empty_name_and_bad_pagination_are_422(api: Api, superadmin: Tokens, run_id: str) -> None:
    expect(api.post("/tenants", superadmin.access, json={"name": "", "slug": f"ok-{run_id}"}), 422)
    expect(api.get("/tenants", superadmin.access, params={"page": 0}), 422)


# --- baseline roles for new tenants ---------------------------------------------------------------


def test_a_new_tenant_comes_with_its_baseline_roles(
    api: Api, superadmin: Tokens, run_id: str
) -> None:
    """Before: `default_role_missing` -- a new tenant's users got no role, and nothing could
    assign one. Now self-registration lands on the `user` role."""
    with _tenant(api, superadmin, run_id) as tenant:
        email = f"member.{run_id}.{uuid.uuid4().hex[:4]}@example.com"
        _register_in(api, tenant["id"], email)
        tokens = api.tokens(email, STRONG_PASSWORD, tenant["id"])

        me = data(expect(api.get("/auth/me", tokens.access), 200))

        assert me["activeRole"] == "user"
        expect(api.get("/users", tokens.access), 403)  # `user` carries no permissions


# --- isolation -------------------------------------------------------------------------------


def test_tenants_are_isolated_from_each_other(
    api: Api, superadmin: Tokens, roles: dict[str, str], run_id: str
) -> None:
    with _tenant(api, superadmin, run_id) as tenant_b:
        email = f"b.{run_id}.{uuid.uuid4().hex[:4]}@example.com"
        b_id = _register_in(api, tenant_b["id"], email)

        expect(api.login(email, STRONG_PASSWORD, tenant_b["id"]), 200)
        expect(api.login(email, STRONG_PASSWORD, TENANT_ID), 401)  # B's credentials, A's door
        expect(api.get(f"/users/{b_id}", superadmin.access), 404)
        expect(api.patch(f"/users/{b_id}", superadmin.access, json={"fullName": "pwned"}), 404)
        expect(api.delete(f"/users/{b_id}", superadmin.access), 404)
        expect(
            api.post(f"/roles/users/{b_id}", superadmin.access, json={"roleId": roles["user"]}), 404
        )
        listed = data(api.get("/users", superadmin.access, params={"page_size": 200}))
        assert b_id not in [u["id"] for u in listed]


# --- lifecycle -------------------------------------------------------------------------------


def test_suspending_a_tenant_locks_out_its_users_until_it_is_reactivated(
    api: Api, admin: Tokens, superadmin: Tokens, run_id: str
) -> None:
    with _tenant(api, superadmin, run_id) as tenant:
        email = f"s.{run_id}.{uuid.uuid4().hex[:4]}@example.com"
        _register_in(api, tenant["id"], email)
        live = api.tokens(email, STRONG_PASSWORD, tenant["id"])
        path = f"/tenants/{tenant['id']}"

        first = expect(api.post(f"{path}/deactivate", superadmin.access), 200)
        again = expect(api.post(f"{path}/deactivate", superadmin.access), 200)  # idempotent
        assert data(first)["isActive"] is False and data(again)["isActive"] is False

        assert api.login(email, STRONG_PASSWORD, tenant["id"]).status_code in (401, 403)
        expect(api.get("/auth/me", live.access), 401, "invalid_token")
        assert api.post("/auth/refresh", json={"refreshToken": live.refresh}).status_code in (
            401,
            403,
        )
        expect(
            api.post(
                "/auth/register",
                json={
                    "tenantId": tenant["id"],
                    "email": f"late.{run_id}@example.com",
                    "password": STRONG_PASSWORD,
                    "fullName": "Late",
                },
            ),
            404,
        )

        restored = expect(api.post(f"{path}/activate", superadmin.access), 200)
        expect(api.post(f"{path}/activate", superadmin.access), 200)  # idempotent
        assert data(restored)["isActive"] is True
        expect(api.login(email, STRONG_PASSWORD, tenant["id"]), 200)


def test_a_superadmin_cannot_suspend_or_delete_their_own_tenant(
    api: Api, superadmin: Tokens
) -> None:
    expect(
        api.post(f"/tenants/{TENANT_ID}/deactivate", superadmin.access),
        403,
        "cannot_modify_own_tenant",
    )
    expect(api.delete(f"/tenants/{TENANT_ID}", superadmin.access), 403, "cannot_modify_own_tenant")


def test_lifecycle_endpoints_404_for_unknown_tenants(api: Api, superadmin: Tokens) -> None:
    unknown = uuid.uuid4()
    expect(api.post(f"/tenants/{unknown}/deactivate", superadmin.access), 404)
    expect(api.post(f"/tenants/{unknown}/activate", superadmin.access), 404)


def test_deleting_a_tenant_is_a_soft_delete_that_frees_the_slug(
    api: Api, superadmin: Tokens, run_id: str
) -> None:
    slug = f"del-{run_id}-{uuid.uuid4().hex[:5]}"
    tenant = data(
        expect(api.post("/tenants", superadmin.access, json={"name": "Doomed", "slug": slug}), 201)
    )
    email = f"d.{run_id}.{uuid.uuid4().hex[:4]}@example.com"
    _register_in(api, tenant["id"], email)
    live = api.tokens(email, STRONG_PASSWORD, tenant["id"])

    expect(api.delete(f"/tenants/{tenant['id']}", superadmin.access), 204)

    assert api.login(email, STRONG_PASSWORD, tenant["id"]).status_code in (401, 403, 404)
    expect(api.get("/auth/me", live.access), 401)
    expect(api.patch(f"/tenants/{tenant['id']}", superadmin.access, json={"name": "x"}), 404)
    expect(api.delete(f"/tenants/{tenant['id']}", superadmin.access), 404)
    listed = data(api.get("/tenants", superadmin.access, params={"page_size": 200}))
    assert tenant["id"] not in [t["id"] for t in listed]

    reborn = expect(
        api.post("/tenants", superadmin.access, json={"name": "Reborn", "slug": slug}), 201
    )
    api.delete(f"/tenants/{data(reborn)['id']}", superadmin.access)


def test_a_tenant_admin_account_made_with_make_admin_is_still_not_a_platform_admin(
    api: Api, make_admin: Callable[[], Account]
) -> None:
    admin = make_admin()
    expect(api.get("/tenants", admin.tokens.access), 403)
