"""Transaction Example CRUD: happy path, RBAC, pagination contract, and not-found handling."""

import uuid

from tests.e2e.conftest import Api, Tokens, data, error_code, expect


def test_listing_and_creating_requires_examples_permissions(
    api: Api, plain_user: Tokens, admin: Tokens, run_id: str
) -> None:
    expect(api.get("/examples", plain_user.access), 403)
    expect(
        api.post("/examples", plain_user.access, json={"name": f"e2e-{run_id}"}),
        403,
    )
    expect(api.get("/examples", admin.access), 200)


def test_create_get_update_delete_lifecycle(api: Api, admin: Tokens, run_id: str) -> None:
    created = data(
        expect(
            api.post(
                "/examples",
                admin.access,
                json={
                    "name": f"e2e-{run_id}",
                    "description": "created by e2e suite",
                    "items": ["priority", "reviewed"],
                },
            ),
            201,
        )
    )
    example_id = created["id"]
    assert created["masterExampleId"] is None
    assert {item["label"] for item in created["items"]} == {"priority", "reviewed"}

    fetched = data(expect(api.get(f"/examples/{example_id}", admin.access), 200))
    assert fetched["id"] == example_id

    updated = data(
        expect(
            api.patch(f"/examples/{example_id}", admin.access, json={"items": ["priority"]}),
            200,
        )
    )
    assert [item["label"] for item in updated["items"]] == ["priority"]
    # Omitted fields on a PATCH are left unchanged, not cleared.
    assert updated["name"] == created["name"]

    expect(api.delete(f"/examples/{example_id}", admin.access), 204)
    expect(api.get(f"/examples/{example_id}", admin.access), 404, "example_not_found")


def test_list_is_paginated(api: Api, admin: Tokens) -> None:
    response = data(expect(api.get("/examples", admin.access, params={"page_size": 1}), 200))
    assert isinstance(response, list)
    assert len(response) <= 1


def test_unknown_example_id_is_not_found(api: Api, admin: Tokens) -> None:
    missing = uuid.uuid4()
    expect(api.get(f"/examples/{missing}", admin.access), 404, "example_not_found")


def test_create_validation_rejects_empty_name(api: Api, admin: Tokens) -> None:
    response = api.post("/examples", admin.access, json={"name": ""})
    expect(response, 422, "validation_error")
    assert error_code(response) == "validation_error"
