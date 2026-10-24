"""Audit log: every mutating action is recorded (with an actor), the log is read-only, filterable,
newest-first, and carries no secrets."""

import json
import time
import uuid
from collections.abc import Callable
from typing import Any

import pytest

from tests.e2e.conftest import Account, Api, Tokens, data, expect


def _entries(api: Api, token: str, **params: Any) -> list[dict[str, Any]]:
    response = expect(api.get("/audit-logs", token, params={"page_size": 200, **params}), 200)
    return list(data(response))


def test_audit_log_requires_audit_log_read(api: Api, plain_user: Tokens, admin: Tokens) -> None:
    assert api.get("/audit-logs").status_code in (401, 403)
    expect(api.get("/audit-logs", plain_user.access), 403)
    expect(api.get("/audit-logs", admin.access), 200)


def test_every_mutating_action_is_recorded_with_its_actor(
    api: Api,
    superadmin: Tokens,
    make_admin: Callable[[], Account],
    make_user: Callable[..., Account],
) -> None:
    admin = make_admin()
    target = make_user()
    role = data(
        expect(
            api.post(
                "/roles",
                admin.tokens.access,
                json={"name": f"aud-{uuid.uuid4().hex[:8]}", "permissions": ["users:read"]},
            ),
            201,
        )
    )
    token = admin.tokens.access
    # Every call below checks its status code: a transient permission/outage/rate-limit hiccup
    # must fail loudly right here, at the action that actually failed, instead of surfacing many
    # lines later as a confusing "audit entry missing" assertion. A status check at each step
    # pins down which call failed.
    expect(api.patch(f"/users/{target.id}", token, json={"fullName": "Audited"}), 200)
    expect(api.post(f"/roles/users/{target.id}", token, json={"roleId": role["id"]}), 204)
    expect(api.delete(f"/roles/users/{target.id}/{role['id']}", token), 204)
    expect(api.patch(f"/roles/{role['id']}", token, json={"description": "changed"}), 200)
    expect(api.delete(f"/roles/{role['id']}", token), 204)
    expect(api.post(f"/users/{target.id}/deactivate", token), 200)
    expect(api.post(f"/users/{target.id}/activate", token), 200)
    expect(api.delete(f"/users/{target.id}", token), 204)
    expect(api.post("/auth/switch-role", token, json={"roleName": "admin"}), 200)
    expect(api.post("/auth/logout", json={"refreshToken": admin.tokens.refresh}), 204)

    expected_actions = {
        "role.created",
        "user.updated",
        "role.assigned",
        "role.revoked",
        "role.updated",
        "role.deleted",
        "user.deactivated",
        "user.activated",
        "user.deleted",
        "auth.role_switched",
        "auth.logout",
    }
    # (`admin.tokens` was invalidated by the logout above, hence reading with the superadmin.)
    # Each write above already committed before its 2xx response was returned, so this read is
    # expected to see all of them immediately — but a handful of bounded retries make the test
    # tolerant of the kind of once-in-thousands read-after-write timing skew observed in CI,
    # without masking a genuine missing entry (the assert below still runs un-retried last).
    actions: set[str] = set()
    for _ in range(5):
        actions = {e["action"] for e in _entries(api, superadmin.access, actor_id=admin.id)}
        if expected_actions <= actions:
            break
        time.sleep(0.2)
    assert expected_actions <= actions


def test_registration_login_and_tenant_events_are_recorded(
    api: Api, superadmin: Tokens, make_user: Callable[..., Account], run_id: str
) -> None:
    account = make_user()
    slug = f"aud-{run_id}-{uuid.uuid4().hex[:5]}"
    tenant = data(
        expect(api.post("/tenants", superadmin.access, json={"name": "Aud", "slug": slug}), 201)
    )
    api.post(f"/tenants/{tenant['id']}/deactivate", superadmin.access)
    api.post(f"/tenants/{tenant['id']}/activate", superadmin.access)
    api.delete(f"/tenants/{tenant['id']}", superadmin.access)

    entries = _entries(api, superadmin.access)
    by_resource = {e["resourceId"]: e for e in entries if e["action"] in {"user.registered"}}
    assert account.id in by_resource
    tenant_actions = {e["action"] for e in entries if e["resourceId"] == tenant["id"]}
    assert {
        "tenant.created",
        "tenant.deactivated",
        "tenant.activated",
        "tenant.deleted",
    } <= tenant_actions
    assert any(e["action"] == "auth.login" for e in entries)


def test_entry_shape_ordering_and_no_secret_material(api: Api, admin: Tokens) -> None:
    entries = _entries(api, admin.access)
    assert entries
    assert {"action", "resourceType", "resourceId", "actorId", "createdAt"} <= set(entries[0])

    stamps = [e["createdAt"] for e in entries]
    assert stamps == sorted(stamps, reverse=True)  # newest first

    blob = json.dumps(entries).lower()
    for forbidden in ("argon2", "hashedpassword", "accesstoken", "refreshtoken", "password"):
        assert forbidden not in blob


def test_filters_and_pagination(api: Api, admin: Tokens, make_user: Callable[..., Account]) -> None:
    make_user()

    only_users = _entries(api, admin.access, resource_type="user")
    assert only_users and all(e["resourceType"] == "user" for e in only_users)
    first_actor = only_users[0]["actorId"]
    if first_actor:
        by_actor = _entries(api, admin.access, actor_id=first_actor)
        assert by_actor and all(e["actorId"] == first_actor for e in by_actor)
    assert _entries(api, admin.access, resource_type="does-not-exist") == []
    assert len(_entries(api, admin.access, page_size=5, page=2)) <= 5
    expect(
        api.get("/audit-logs", admin.access, params={"actor_id": "nope"}), 422, "validation_error"
    )


def test_sqlish_filter_values_are_treated_as_data(api: Api, admin: Tokens) -> None:
    assert _entries(api, admin.access, resource_type="' OR 1=1 --") == []


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_audit_log_is_read_only(api: Api, superadmin: Tokens, method: str) -> None:
    response = api.request(method, "/audit-logs", superadmin.access, json={"action": "x"})
    expect(response, 405)
