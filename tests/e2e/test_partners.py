"""Partner (API-key-authenticated) surface: `X-API-Key` auth, scope checks, never JWT/RBAC.

Uses `superadmin` to issue the API keys under test — see `test_api_clients.py`'s module
docstring for why (seed-data freshness of tenant-level roles shouldn't be assumed here)."""

from tests.e2e.conftest import Api, Tokens, data, expect


def _issue_key(api: Api, superadmin: Tokens, run_id: str, scopes: list[str]) -> str:
    created = data(
        api.post(
            "/api-clients",
            superadmin.access,
            json={"name": f"e2e-partner-{run_id}", "scopes": scopes},
        )
    )
    return str(created["plaintextKey"])


def test_valid_key_with_scope_can_list_and_fetch_posts(
    api: Api, superadmin: Tokens, run_id: str
) -> None:
    key = _issue_key(api, superadmin, run_id, ["posts:read"])
    headers = {"X-API-Key": key}

    listed = expect(api.get("/partners/posts", headers=headers), 200)
    posts = data(listed)
    assert isinstance(posts, list)

    if posts:
        post_id = posts[0]["id"]
        expect(api.get(f"/partners/posts/{post_id}", headers=headers), 200)


def test_missing_or_invalid_key_is_unauthorized(api: Api) -> None:
    expect(api.get("/partners/posts"), 401, "invalid_api_key")
    expect(
        api.get("/partners/posts", headers={"X-API-Key": "not-a-real-key"}), 401, "invalid_api_key"
    )


def test_key_without_the_required_scope_is_forbidden(
    api: Api, superadmin: Tokens, run_id: str
) -> None:
    key = _issue_key(api, superadmin, run_id, [])  # no scopes granted
    response = api.get("/partners/posts", headers={"X-API-Key": key})
    expect(response, 403, "scope_denied")


def test_revoked_key_is_rejected(api: Api, superadmin: Tokens, run_id: str) -> None:
    created = data(
        api.post(
            "/api-clients",
            superadmin.access,
            json={"name": f"e2e-partner-revoked-{run_id}", "scopes": ["posts:read"]},
        )
    )
    key_id, plaintext = created["id"], created["plaintextKey"]
    expect(api.post(f"/api-clients/{key_id}/revoke", superadmin.access), 200)

    # Revoke clears both key_hash columns (see RevokeApiKeyUseCase), so the plaintext no
    # longer matches any stored hash at all — the lookup fails as "invalid", not "revoked".
    response = api.get("/partners/posts", headers={"X-API-Key": plaintext})
    expect(response, 401, "invalid_api_key")


def test_jwt_bearer_token_does_not_authenticate_the_partner_surface(
    api: Api, superadmin: Tokens
) -> None:
    """Partners endpoints are API-key-only — a JWT access token must not work here."""
    expect(api.get("/partners/posts", superadmin.access), 401, "invalid_api_key")


def test_each_resource_answers_only_to_its_own_scope(
    api: Api, superadmin: Tokens, run_id: str
) -> None:
    key = _issue_key(api, superadmin, run_id, ["users:read", "examples:read"])
    headers = {"X-API-Key": key}

    assert isinstance(data(expect(api.get("/partners/users", headers=headers), 200)), list)
    assert isinstance(data(expect(api.get("/partners/examples", headers=headers), 200)), list)
    expect(api.get("/partners/roles", headers=headers), 403, "scope_denied")
    expect(api.get("/partners/master/examples", headers=headers), 403, "scope_denied")
