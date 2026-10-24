"""Posts — external-API (JSONPlaceholder) CRUD demo, proxied end-to-end through the real stack.

This suite depends on jsonplaceholder.typicode.com being reachable from the `api` container
(configured via `posts_api_base_url`); it only proves the route/RBAC/audit wiring end-to-end —
`tests/unit/test_posts_adapter.py` covers the adapter's own failure-handling branches with mocks.

Uses `superadmin` (not `admin`) for the same reason as `test_api_clients.py`: whether a
tenant-level role actually holds `posts:*` is seed data, not something this suite should assume.
"""

from tests.e2e.conftest import Api, Tokens, data, error_code, expect


def test_listing_and_creating_requires_posts_permissions(
    api: Api, plain_user: Tokens, superadmin: Tokens
) -> None:
    expect(api.get("/posts", plain_user.access), 403)
    expect(
        api.post(
            "/posts",
            plain_user.access,
            json={"userId": 1, "title": "t", "body": "b"},
        ),
        403,
    )
    expect(api.get("/posts", superadmin.access), 200)


def test_get_an_existing_post(api: Api, superadmin: Tokens) -> None:
    post = data(expect(api.get("/posts/1", superadmin.access), 200))
    assert post["id"] == 1
    assert post["userId"] > 0
    assert post["title"]


def test_unknown_post_is_not_found(api: Api, superadmin: Tokens) -> None:
    expect(api.get("/posts/999999999", superadmin.access), 404, "post_not_found")


def test_create_returns_the_echoed_post(api: Api, superadmin: Tokens, run_id: str) -> None:
    created = data(
        expect(
            api.post(
                "/posts",
                superadmin.access,
                json={"userId": 1, "title": f"e2e-{run_id}", "body": "created by e2e suite"},
            ),
            201,
        )
    )
    assert created["title"] == f"e2e-{run_id}"


def test_update_and_delete_an_existing_post(api: Api, superadmin: Tokens) -> None:
    # JSONPlaceholder doesn't persist writes (see the adapter's module docstring), and
    # update/delete fetch-check-first — so only an id that GET already knows about (1-100)
    # can round-trip a PATCH/DELETE here; a freshly-created post's id would 404 on that check.
    updated = data(
        expect(api.patch("/posts/1", superadmin.access, json={"title": "updated title"}), 200)
    )
    assert updated["title"] == "updated title"

    expect(api.delete("/posts/1", superadmin.access), 204)


def test_create_validation_rejects_empty_title(api: Api, superadmin: Tokens) -> None:
    response = api.post("/posts", superadmin.access, json={"userId": 1, "title": "", "body": "b"})
    expect(response, 422, "validation_error")
    assert error_code(response) == "validation_error"
