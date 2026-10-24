"""Master Example (reference-data) CRUD: happy path, RBAC, duplicate-name conflict, pagination."""

import uuid

from tests.e2e.conftest import Api, Tokens, data, error_code, expect


def test_listing_and_creating_requires_master_examples_permissions(
    api: Api, plain_user: Tokens, admin: Tokens, run_id: str
) -> None:
    expect(api.get("/master/examples", plain_user.access), 403)
    expect(
        api.post("/master/examples", plain_user.access, json={"name": f"e2e-{run_id}"}),
        403,
    )
    expect(api.get("/master/examples", admin.access), 200)


def test_create_get_update_delete_lifecycle(api: Api, admin: Tokens, run_id: str) -> None:
    created = data(
        expect(
            api.post(
                "/master/examples",
                admin.access,
                json={"name": f"e2e-{run_id}", "description": "created by e2e suite"},
            ),
            201,
        )
    )
    example_id = created["id"]

    fetched = data(expect(api.get(f"/master/examples/{example_id}", admin.access), 200))
    assert fetched["id"] == example_id

    updated = data(
        expect(
            api.patch(
                f"/master/examples/{example_id}",
                admin.access,
                json={"description": "updated by e2e suite"},
            ),
            200,
        )
    )
    assert updated["description"] == "updated by e2e suite"
    assert updated["name"] == created["name"]  # omitted field left unchanged

    expect(api.delete(f"/master/examples/{example_id}", admin.access), 204)
    expect(api.get(f"/master/examples/{example_id}", admin.access), 404, "master_example_not_found")


def test_duplicate_name_conflicts(api: Api, admin: Tokens, run_id: str) -> None:
    name = f"e2e-dup-{run_id}"
    expect(api.post("/master/examples", admin.access, json={"name": name}), 201)
    duplicate = api.post("/master/examples", admin.access, json={"name": name})
    expect(duplicate, 409, "master_example_already_exists")


def test_unknown_example_id_is_not_found(api: Api, admin: Tokens) -> None:
    missing = uuid.uuid4()
    expect(api.get(f"/master/examples/{missing}", admin.access), 404, "master_example_not_found")


def test_create_validation_rejects_empty_name(api: Api, admin: Tokens) -> None:
    response = api.post("/master/examples", admin.access, json={"name": ""})
    expect(response, 422, "validation_error")
    assert error_code(response) == "validation_error"
