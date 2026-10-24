"""Users: RBAC, pagination bounds, profile validation, and the deactivate / activate / delete
lifecycle (including the guards against locking yourself out)."""

import uuid
from collections.abc import Callable

import pytest

from tests.e2e.conftest import (
    STRONG_PASSWORD,
    TENANT_ID,
    Account,
    Api,
    Tokens,
    data,
    expect,
)

# --- listing, RBAC, pagination ---------------------------------------------------------------


def test_listing_users_requires_the_users_read_permission(
    api: Api, plain_user: Tokens, admin: Tokens
) -> None:
    assert api.get("/users").status_code in (401, 403)
    expect(api.get("/users", plain_user.access), 403, "insufficient_permission")
    expect(api.get("/users", admin.access), 200)


def test_user_list_shape_hides_secrets_and_paginates(api: Api, admin: Tokens) -> None:
    response = expect(api.get("/users", admin.access), 200)
    body = response.json()

    assert body["meta"]["pagination"]["total"] >= 4
    assert "fullName" in body["data"][0]
    assert "hashed" not in response.text.lower() and "password" not in response.text.lower()


def test_pagination_walks_pages_without_overlap(api: Api, admin: Tokens) -> None:
    first = expect(api.get("/users", admin.access, params={"page": 1, "page_size": 2}), 200)
    second = expect(api.get("/users", admin.access, params={"page": 2, "page_size": 2}), 200)

    assert len(data(first)) == 2
    assert first.json()["meta"]["pagination"]["totalPages"] >= 2
    assert {u["id"] for u in data(first)}.isdisjoint({u["id"] for u in data(second)})


@pytest.mark.parametrize(
    "params",
    [
        {"page": 0},
        {"page": -1},
        {"page": "abc"},
        {"page_size": 0},
        {"page_size": 201},
        {"page": 1_000_001},  # over the bound
        {"page": 10**30},  # used to overflow int64 in Postgres -> 500
        {"page": 10**9},
        {"page": "1;DROP TABLE"},
    ],
)
def test_invalid_pagination_is_422_never_500(
    api: Api, admin: Tokens, params: dict[str, object]
) -> None:
    expect(api.get("/users", admin.access, params=params), 422, "validation_error")


def test_the_last_allowed_page_is_simply_empty(api: Api, admin: Tokens) -> None:
    response = expect(api.get("/users", admin.access, params={"page": 1_000_000}), 200)
    assert data(response) == []


# --- get / patch -----------------------------------------------------------------------------


def test_get_user(
    api: Api, admin: Tokens, plain_user: Tokens, make_user: Callable[..., Account]
) -> None:
    account = make_user()

    expect(api.get(f"/users/{account.id}", admin.access), 200)
    expect(api.get(f"/users/{uuid.uuid4()}", admin.access), 404, "user_not_found")
    expect(api.get("/users/not-a-uuid", admin.access), 422, "validation_error")
    expect(api.get(f"/users/{account.id}", plain_user.access), 403)


def test_patch_applies_valid_profile_fields(
    api: Api, admin: Tokens, make_user: Callable[..., Account]
) -> None:
    account = make_user()

    response = expect(
        api.patch(
            f"/users/{account.id}",
            admin.access,
            json={"fullName": "Renamed", "locale": "id", "timezone": "Asia/Jakarta"},
        ),
        200,
    )

    assert (data(response)["fullName"], data(response)["locale"], data(response)["timezone"]) == (
        "Renamed",
        "id",
        "Asia/Jakarta",
    )


@pytest.mark.parametrize(
    "payload",
    [{"locale": "xx"}, {"timezone": "Mars/Olympus"}, {"fullName": ""}],
)
def test_patch_rejects_invalid_values_and_persists_nothing(
    api: Api, admin: Tokens, make_user: Callable[..., Account], payload: dict[str, str]
) -> None:
    account = make_user()
    before = data(api.get(f"/users/{account.id}", admin.access))

    expect(api.patch(f"/users/{account.id}", admin.access, json=payload), 422, "validation_error")

    assert data(api.get(f"/users/{account.id}", admin.access)) == before


def test_patch_ignores_mass_assignment_and_accepts_an_empty_body(
    api: Api, admin: Tokens, make_user: Callable[..., Account]
) -> None:
    account = make_user()

    hijack = expect(
        api.patch(
            f"/users/{account.id}",
            admin.access,
            json={"email": "hijack@example.com", "isActive": False, "hashedPassword": "x"},
        ),
        200,
    )
    assert data(hijack)["email"] == account.email and data(hijack)["isActive"] is True
    expect(api.patch(f"/users/{account.id}", admin.access, json={}), 200)


def test_patch_permissions_and_unknown_user(
    api: Api, admin: Tokens, plain_user: Tokens, make_user: Callable[..., Account]
) -> None:
    account = make_user()
    expect(api.patch(f"/users/{account.id}", plain_user.access, json={"fullName": "x"}), 403)
    expect(api.patch(f"/users/{uuid.uuid4()}", admin.access, json={"fullName": "x"}), 404)


def test_saved_locale_drives_error_language_and_the_header_overrides_it(
    api: Api, admin: Tokens, make_user: Callable[..., Account]
) -> None:
    account = make_user()
    api.patch(f"/users/{account.id}", admin.access, json={"locale": "id"})

    saved = api.get("/users", account.tokens.access).json()["error"]["message"]
    overridden = api.get("/users", account.tokens.access, headers={"Accept-Language": "en"}).json()[
        "error"
    ]["message"]

    assert saved != overridden  # the saved locale (id) translated the message...
    assert overridden.startswith("You do not have")  # ...and Accept-Language: en overrode it


# --- deactivate / activate / delete ----------------------------------------------------------


def test_deactivate_requires_users_write_and_blocks_the_account(
    api: Api,
    plain_user: Tokens,
    make_admin: Callable[[], Account],
    make_user: Callable[..., Account],
) -> None:
    admin = make_admin()
    victim = make_user()
    expect(api.post(f"/users/{victim.id}/deactivate", plain_user.access), 403)

    first = expect(api.post(f"/users/{victim.id}/deactivate", admin.tokens.access), 200)
    again = expect(api.post(f"/users/{victim.id}/deactivate", admin.tokens.access), 200)

    assert data(first)["isActive"] is False and data(again)["isActive"] is False
    expect(api.login(victim.email, victim.password), 403, "account_disabled")
    expect(api.get("/auth/me", victim.tokens.access), 401, "invalid_token")
    expect(api.post("/auth/refresh", json={"refreshToken": victim.tokens.refresh}), 401)
    expect(api.post(f"/users/{uuid.uuid4()}/deactivate", admin.tokens.access), 404)


def test_activate_restores_a_deactivated_user(
    api: Api,
    plain_user: Tokens,
    make_admin: Callable[[], Account],
    make_user: Callable[..., Account],
) -> None:
    admin = make_admin()
    victim = make_user()
    api.post(f"/users/{victim.id}/deactivate", admin.tokens.access)
    expect(api.post(f"/users/{victim.id}/activate", plain_user.access), 403)

    restored = expect(api.post(f"/users/{victim.id}/activate", admin.tokens.access), 200)
    again = expect(api.post(f"/users/{victim.id}/activate", admin.tokens.access), 200)  # idempotent

    assert data(restored)["isActive"] is True and data(again)["isActive"] is True
    expect(api.login(victim.email, victim.password), 200)
    expect(api.post(f"/users/{uuid.uuid4()}/activate", admin.tokens.access), 404, "user_not_found")


def test_an_admin_cannot_deactivate_or_delete_themselves(
    api: Api, make_admin: Callable[[], Account]
) -> None:
    admin = make_admin()

    expect(
        api.post(f"/users/{admin.id}/deactivate", admin.tokens.access), 409, "cannot_modify_self"
    )
    expect(api.delete(f"/users/{admin.id}", admin.tokens.access), 409, "cannot_modify_self")

    expect(api.get("/auth/me", admin.tokens.access), 200)  # still able to sign in
    expect(api.login(admin.email, admin.password), 200)


def test_delete_soft_deletes_and_frees_the_email(
    api: Api,
    plain_user: Tokens,
    make_admin: Callable[[], Account],
    make_user: Callable[..., Account],
) -> None:
    admin = make_admin()
    victim = make_user()
    expect(api.delete(f"/users/{victim.id}", plain_user.access), 403)

    expect(api.delete(f"/users/{victim.id}", admin.tokens.access), 204)

    expect(api.get(f"/users/{victim.id}", admin.tokens.access), 404)
    expect(api.delete(f"/users/{victim.id}", admin.tokens.access), 404)
    expect(api.get("/auth/me", victim.tokens.access), 401, "invalid_token")
    expect(api.post("/auth/refresh", json={"refreshToken": victim.tokens.refresh}), 401)
    # the soft-deleted user's email is reusable (partial unique index)
    reborn = expect(
        api.post(
            "/auth/register",
            json={
                "tenantId": TENANT_ID,
                "email": victim.email,
                "password": STRONG_PASSWORD,
                "fullName": "Reborn",
            },
        ),
        201,
    )
    assert data(reborn)["id"] != victim.id
    expect(api.login(victim.email, STRONG_PASSWORD), 200)
