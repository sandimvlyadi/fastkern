"""Auth flows: register, login, /me, refresh, logout and switch-role -- including the session
revocation, refresh-token reuse detection, email-case and account-disabled behaviour."""

import json
import statistics
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from tests.e2e.conftest import (
    SECRET_KEY,
    STRONG_PASSWORD,
    TENANT_ID,
    Account,
    Api,
    data,
    error_code,
    expect,
    forge_access_token,
)

# --- register ------------------------------------------------------------------------------


def test_register_happy_path_returns_the_public_fields_only(api: Api, run_id: str) -> None:
    response = expect(
        api.post(
            "/auth/register",
            json={
                "tenantId": TENANT_ID,
                "email": f"reg.{run_id}@example.com",
                "password": STRONG_PASSWORD,
                "fullName": "Reg Tester",
            },
        ),
        201,
    )

    body = data(response)
    assert {"id", "email", "fullName", "isActive"} <= set(body)
    assert body["isActive"] is True
    assert "password" not in json.dumps(body).lower()


def test_register_stores_the_email_lowercased_and_rejects_case_variants(
    api: Api, run_id: str
) -> None:
    mixed = f"Case.{run_id}@Example.COM"
    payload = {"tenantId": TENANT_ID, "password": STRONG_PASSWORD, "fullName": "Case"}

    first = expect(api.post("/auth/register", json={**payload, "email": mixed}), 201)
    lower = api.post("/auth/register", json={**payload, "email": mixed.lower()})
    upper = api.post("/auth/register", json={**payload, "email": mixed.upper()})

    assert data(first)["email"] == mixed.lower()
    expect(lower, 409, "email_already_registered")
    expect(upper, 409, "email_already_registered")


def test_register_duplicate_email_is_a_conflict(
    api: Api, make_user: Callable[..., Account]
) -> None:
    account = make_user()
    response = api.post(
        "/auth/register",
        json={
            "tenantId": TENANT_ID,
            "email": account.email,
            "password": STRONG_PASSWORD,
            "fullName": "Dup",
        },
    )
    expect(response, 409, "email_already_registered")


def test_register_unknown_tenant_is_404(api: Api, run_id: str) -> None:
    response = api.post(
        "/auth/register",
        json={
            "tenantId": str(uuid.uuid4()),
            "email": f"ghost.{run_id}@example.com",
            "password": STRONG_PASSWORD,
            "fullName": "Ghost",
        },
    )
    expect(response, 404, "tenant_not_found")


@pytest.mark.parametrize(
    ("override", "why"),
    [
        ({"email": "not-an-email"}, "invalid email"),
        ({"password": "Sh0rt!x"}, "short password"),  # distinctive: must not be echoed back
        ({"password": "x" * 200}, "password over 128"),
        ({"fullName": ""}, "empty name"),
        ({"fullName": "A" * 256}, "name over 255"),
        ({"tenantId": "not-a-uuid"}, "bad tenant uuid"),
    ],
)
def test_register_validation_errors_are_422_and_never_echo_the_input(
    api: Api, run_id: str, override: dict[str, str], why: str
) -> None:
    payload = {
        "tenantId": TENANT_ID,
        "email": f"val.{run_id}@example.com",
        "password": STRONG_PASSWORD,
        "fullName": "Val",
        **override,
    }
    response = api.post("/auth/register", json=payload)

    expect(response, 422, "validation_error")
    errors = response.json()["error"]["details"]["errors"]
    assert errors and all(set(error) == {"type", "loc", "msg"} for error in errors), why
    assert payload["password"] not in response.text


def test_register_empty_or_malformed_body_is_422(api: Api) -> None:
    expect(api.post("/auth/register", json={}), 422, "validation_error")
    malformed = api.post(
        "/auth/register", content=b"{not json", headers={"Content-Type": "application/json"}
    )
    expect(malformed, 422)


def test_register_accepts_unicode_and_stores_sqlish_names_literally(api: Api, run_id: str) -> None:
    for name in ("Ünïcödé 日本語 🚀", "Robert'); DROP TABLE sys_users;--"):
        response = expect(
            api.post(
                "/auth/register",
                json={
                    "tenantId": TENANT_ID,
                    "email": f"n{uuid.uuid4().hex[:6]}.{run_id}@example.com",
                    "password": STRONG_PASSWORD,
                    "fullName": name,
                },
            ),
            201,
        )
        assert data(response)["fullName"] == name


def test_register_ignores_mass_assignment(api: Api, run_id: str) -> None:
    response = expect(
        api.post(
            "/auth/register",
            json={
                "tenantId": TENANT_ID,
                "email": f"mass.{run_id}@example.com",
                "password": STRONG_PASSWORD,
                "fullName": "Mass",
                "isActive": False,
                "id": str(uuid.uuid4()),
                "roles": ["superadmin"],
            },
        ),
        201,
    )
    assert data(response)["isActive"] is True


# --- login ---------------------------------------------------------------------------------


def test_login_happy_path(api: Api, make_user: Callable[..., Account]) -> None:
    account = make_user()

    response = expect(api.login(account.email, account.password), 200)

    body = data(response)
    assert body["tokenType"] == "bearer"
    assert body["accessToken"] and body["refreshToken"]


def test_login_wrong_password_and_unknown_user_are_indistinguishable(
    api: Api, make_user: Callable[..., Account], run_id: str
) -> None:
    account = make_user()

    wrong = expect(api.login(account.email, "WrongPass123!"), 401, "invalid_credentials")
    unknown = expect(api.login(f"nobody.{run_id}@example.com", "WrongPass123!"), 401)

    assert error_code(unknown) == "invalid_credentials"
    assert wrong.json()["error"]["message"] == unknown.json()["error"]["message"]


def test_login_timing_does_not_reveal_which_emails_exist(
    api: Api, make_user: Callable[..., Account], run_id: str
) -> None:
    account = make_user()

    def median_seconds(email: str) -> float:
        samples = []
        for _ in range(5):
            started = time.perf_counter()
            api.login(email, "WrongPass123!")
            samples.append(time.perf_counter() - started)
        return statistics.median(samples)

    unknown = median_seconds(f"nobody.{run_id}@example.com")
    wrong = median_seconds(account.email)

    # Before the fix an unknown email answered ~20x faster; a loose factor keeps this stable.
    assert unknown >= 0.3 * wrong, f"unknown={unknown:.3f}s wrong-password={wrong:.3f}s"


def test_login_wrong_tenant_is_401(api: Api, make_user: Callable[..., Account]) -> None:
    account = make_user()
    expect(api.login(account.email, account.password, tenant_id=str(uuid.uuid4())), 401)


@pytest.mark.parametrize("variant", [str.upper, str.title, lambda e: f"  {e} "])
def test_login_ignores_email_case_and_padding(
    api: Api, make_user: Callable[..., Account], variant: Callable[[str], str]
) -> None:
    account = make_user()
    expect(api.login(variant(account.email), account.password), 200)


def test_login_missing_fields_is_422(api: Api) -> None:
    expect(api.post("/auth/login", json={"email": "a@example.com"}), 422, "validation_error")


def test_error_messages_follow_accept_language_with_english_fallback(api: Api) -> None:
    en = api.login("nobody@example.com", "bad").json()["error"]["message"]
    id_ = api.login("nobody@example.com", "bad", headers={"Accept-Language": "id"}).json()["error"][
        "message"
    ]
    fr = api.login("nobody@example.com", "bad", headers={"Accept-Language": "fr"}).json()["error"][
        "message"
    ]

    assert id_ != en
    assert fr == en


def test_deactivated_account_is_403_until_reactivated(
    api: Api, make_user: Callable[..., Account], make_admin: Callable[[], Account]
) -> None:
    admin = make_admin()
    account = make_user()
    api.post(f"/users/{account.id}/deactivate", admin.tokens.access)

    disabled = api.login(account.email, account.password)
    # 403, not 401: the credentials were right, the account is what's refused.
    expect(disabled, 403, "account_disabled")

    expect(api.post(f"/users/{account.id}/activate", admin.tokens.access), 200)
    expect(api.login(account.email, account.password), 200)


# --- /me and token validation --------------------------------------------------------------


def test_me_reports_the_user_and_their_active_role(
    api: Api, make_user: Callable[..., Account]
) -> None:
    account = make_user()

    response = expect(api.get("/auth/me", account.tokens.access), 200)

    assert data(response)["email"] == account.email
    assert data(response)["activeRole"] == "user"


@pytest.mark.parametrize("token", [None, "garbage", "a" * 20000])
def test_me_without_a_usable_token_is_rejected(api: Api, token: str | None) -> None:
    response = api.get("/auth/me", token)
    assert response.status_code in (401, 403)


def test_me_rejects_a_refresh_token_and_a_basic_scheme(
    api: Api, make_user: Callable[..., Account]
) -> None:
    account = make_user()
    expect(api.get("/auth/me", account.tokens.refresh), 401, "invalid_token")
    basic = api.get("/auth/me", headers={"Authorization": "Basic abc"})
    assert basic.status_code in (401, 403)


def test_me_rejects_forged_and_malformed_tokens(
    api: Api, make_user: Callable[..., Account]
) -> None:
    account = make_user()
    now = datetime.now(UTC)
    cases = {
        "expired": forge_access_token(account.id, exp=now - timedelta(minutes=1)),
        "no tenant claim": forge_access_token(account.id, tenant_id=None),
        "nonexistent tenant": forge_access_token(account.id, tenant_id=str(uuid.uuid4())),
        "nonexistent user": forge_access_token(str(uuid.uuid4())),
        "wrong signature": jwt.encode(
            {
                "sub": account.id,
                "type": "access",
                "jti": "x",
                "tenant_id": TENANT_ID,
                "exp": now + timedelta(minutes=5),
            },
            "wrong-secret-wrong-secret-wrong-secret-1234",
            algorithm="HS256",
        ),
        "alg none": jwt.encode(
            {"sub": account.id, "type": "access", "jti": "x", "tenant_id": TENANT_ID},
            None,
            algorithm="none",
        ),
    }
    for name, token in cases.items():
        response = api.get("/auth/me", token)
        assert response.status_code == 401, f"{name}: {response.status_code} {response.text[:200]}"
        assert error_code(response) == "invalid_token", name


def test_a_forged_active_role_claim_grants_nothing(
    api: Api, make_user: Callable[..., Account]
) -> None:
    account = make_user()
    forged = forge_access_token(account.id, active_role="superadmin")
    expect(api.get("/users", forged), 403)  # authorisation is checked against the DB, not the claim


# --- refresh -------------------------------------------------------------------------------


def test_refresh_rotates_the_pair(api: Api, make_user: Callable[..., Account]) -> None:
    account = make_user()

    response = expect(api.post("/auth/refresh", json={"refreshToken": account.tokens.refresh}), 200)

    new = data(response)
    assert new["refreshToken"] != account.tokens.refresh
    assert new["accessToken"] != account.tokens.access
    expect(api.get("/auth/me", new["accessToken"]), 200)


def test_refresh_rejects_an_access_token_garbage_and_an_empty_body(
    api: Api, make_user: Callable[..., Account]
) -> None:
    account = make_user()
    expect(
        api.post("/auth/refresh", json={"refreshToken": account.tokens.access}),
        401,
        "invalid_token",
    )
    expect(api.post("/auth/refresh", json={"refreshToken": "garbage"}), 401, "invalid_token")
    expect(api.post("/auth/refresh", json={}), 422, "validation_error")


def test_reusing_a_refresh_token_revokes_the_whole_family(
    api: Api, make_user: Callable[..., Account]
) -> None:
    account = make_user()
    first = data(
        expect(api.post("/auth/refresh", json={"refreshToken": account.tokens.refresh}), 200)
    )
    assert api.get("/auth/me", first["accessToken"]).status_code == 200

    reuse = api.post("/auth/refresh", json={"refreshToken": account.tokens.refresh})

    expect(reuse, 401, "token_revoked")
    # The legitimately-issued child is revoked too, and so is its access token.
    expect(api.post("/auth/refresh", json={"refreshToken": first["refreshToken"]}), 401)
    expect(api.get("/auth/me", first["accessToken"]), 401, "invalid_token")


def test_reuse_in_one_session_leaves_the_users_other_sessions_alone(
    api: Api, make_user: Callable[..., Account]
) -> None:
    account = make_user()
    other = api.tokens(account.email, account.password)  # a second login = a second session
    api.post("/auth/refresh", json={"refreshToken": account.tokens.refresh})
    api.post("/auth/refresh", json={"refreshToken": account.tokens.refresh})  # reuse

    expect(api.get("/auth/me", other.access), 200)
    expect(api.post("/auth/refresh", json={"refreshToken": other.refresh}), 200)


def test_a_garbage_or_foreign_refresh_token_revokes_nothing(
    api: Api, make_user: Callable[..., Account]
) -> None:
    account = make_user()
    expect(api.post("/auth/refresh", json={"refreshToken": "not-a-jwt"}), 401)
    if SECRET_KEY:
        foreign = jwt.encode(
            {
                "sub": account.id,
                "type": "refresh",
                "jti": "x",
                "sid": "whatever",
                "exp": datetime.now(UTC) + timedelta(hours=1),
            },
            "some-other-key-entirely-not-ours-0123456789",
            algorithm="HS256",
        )
        expect(api.post("/auth/refresh", json={"refreshToken": foreign}), 401, "invalid_token")
    expect(api.get("/auth/me", account.tokens.access), 200)


# --- logout --------------------------------------------------------------------------------


def test_logout_revokes_the_refresh_token_and_is_idempotent(
    api: Api, make_user: Callable[..., Account]
) -> None:
    account = make_user()

    expect(api.post("/auth/logout", json={"refreshToken": account.tokens.refresh}), 204)
    expect(
        api.post("/auth/refresh", json={"refreshToken": account.tokens.refresh}),
        401,
        "token_revoked",
    )
    expect(api.post("/auth/logout", json={"refreshToken": account.tokens.refresh}), 204)
    expect(api.post("/auth/logout", json={"refreshToken": "garbage"}), 204)
    expect(api.post("/auth/logout", json={}), 422, "validation_error")


def test_logout_also_kills_the_access_token_of_that_session_only(
    api: Api, make_user: Callable[..., Account]
) -> None:
    account = make_user()
    other = api.tokens(account.email, account.password)
    assert api.get("/auth/me", account.tokens.access).status_code == 200

    expect(api.post("/auth/logout", json={"refreshToken": account.tokens.refresh}), 204)

    expect(api.get("/auth/me", account.tokens.access), 401, "invalid_token")
    expect(api.get("/auth/me", other.access), 200)


# --- switch-role ---------------------------------------------------------------------------


def test_switch_role_to_an_unassigned_role_is_403(
    api: Api, make_user: Callable[..., Account]
) -> None:
    account = make_user()
    expect(
        api.post("/auth/switch-role", account.tokens.access, json={"roleName": "admin"}),
        403,
        "role_not_assigned",
    )


def test_switch_role_to_an_assigned_role_reissues_tokens(
    api: Api, make_user: Callable[..., Account]
) -> None:
    account = make_user()

    response = expect(
        api.post("/auth/switch-role", account.tokens.access, json={"roleName": "user"}), 200
    )

    assert {"accessToken", "refreshToken"} <= set(data(response))


def test_switch_role_validation_and_auth(api: Api, make_user: Callable[..., Account]) -> None:
    account = make_user()
    assert api.post("/auth/switch-role", json={"roleName": "user"}).status_code in (401, 403)
    expect(api.post("/auth/switch-role", account.tokens.access, json={}), 422, "validation_error")


def test_switch_role_stays_in_the_session_so_logout_covers_both_token_pairs(
    api: Api, make_user: Callable[..., Account]
) -> None:
    account = make_user()
    switched = data(
        expect(api.post("/auth/switch-role", account.tokens.access, json={"roleName": "user"}), 200)
    )

    expect(api.post("/auth/logout", json={"refreshToken": switched["refreshToken"]}), 204)

    expect(api.get("/auth/me", account.tokens.access), 401)
    expect(api.get("/auth/me", switched["accessToken"]), 401)
    # switch-role leaves the pre-switch refresh token alive; the session revocation must not.
    expect(
        api.post("/auth/refresh", json={"refreshToken": account.tokens.refresh}),
        401,
        "token_revoked",
    )
