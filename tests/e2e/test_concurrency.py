"""Races against the real Postgres: check-then-insert must end in 409 / an idempotent success,
never a 500, and a refresh token must be consumable exactly once."""

import asyncio
import uuid
from collections import Counter
from collections.abc import Callable
from typing import Any

import httpx

from tests.e2e.conftest import API, STRONG_PASSWORD, TENANT_ID, Account, Tokens, data

PARALLEL = 8


def _statuses(responses: list[httpx.Response]) -> Counter[int]:
    return Counter(r.status_code for r in responses)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=API, timeout=60)


async def _burst(
    client: httpx.AsyncClient, method: str, path: str, count: int = PARALLEL, **kwargs: Any
) -> list[httpx.Response]:
    return list(
        await asyncio.gather(*(client.request(method, path, **kwargs) for _ in range(count)))
    )


async def test_parallel_registration_of_one_email_yields_one_201_and_the_rest_409(
    run_id: str,
) -> None:
    payload = {
        "tenantId": TENANT_ID,
        "email": f"race.{run_id}.{uuid.uuid4().hex[:4]}@example.com",
        "password": STRONG_PASSWORD,
        "fullName": "Racer",
    }
    async with _client() as client:
        responses = await _burst(client, "POST", "/auth/register", json=payload)

    assert _statuses(responses) == {201: 1, 409: PARALLEL - 1}
    assert {r.json()["error"]["code"] for r in responses if r.status_code == 409} == {
        "email_already_registered"
    }


async def test_parallel_refresh_of_one_token_succeeds_exactly_once(
    make_user: Callable[..., Account],
) -> None:
    account = make_user()
    async with _client() as client:
        responses = await _burst(
            client, "POST", "/auth/refresh", count=6, json={"refreshToken": account.tokens.refresh}
        )

    assert _statuses(responses) == {200: 1, 401: 5}


async def test_parallel_role_creation_with_one_name_yields_one_201_and_the_rest_409(
    superadmin: Tokens, run_id: str
) -> None:
    headers = {"Authorization": f"Bearer {superadmin.access}"}
    name = f"race-{run_id}-{uuid.uuid4().hex[:5]}"
    async with _client() as client:
        responses = await _burst(
            client,
            "POST",
            "/roles",
            count=6,
            headers=headers,
            json={"name": name, "permissions": []},
        )
        for response in responses:
            if response.status_code == 201:
                await client.delete(f"/roles/{data(response)['id']}", headers=headers)

    assert _statuses(responses) == {201: 1, 409: 5}
    assert {r.json()["error"]["code"] for r in responses if r.status_code == 409} == {
        "role_already_exists"
    }


async def test_parallel_tenant_creation_with_one_slug_yields_one_201_and_the_rest_409(
    superadmin: Tokens, run_id: str
) -> None:
    headers = {"Authorization": f"Bearer {superadmin.access}"}
    slug = f"race-{run_id}-{uuid.uuid4().hex[:5]}"
    async with _client() as client:
        responses = await _burst(
            client,
            "POST",
            "/tenants",
            count=6,
            headers=headers,
            json={"name": "Race", "slug": slug},
        )
        for response in responses:
            if response.status_code == 201:
                await client.delete(f"/tenants/{data(response)['id']}", headers=headers)

    assert _statuses(responses) == {201: 1, 409: 5}
    assert {r.json()["error"]["code"] for r in responses if r.status_code == 409} == {
        "tenant_slug_already_exists"
    }


async def test_parallel_role_assignment_is_idempotent(
    superadmin: Tokens, make_user: Callable[..., Account], run_id: str
) -> None:
    account = make_user()
    headers = {"Authorization": f"Bearer {superadmin.access}"}
    async with _client() as client:
        created = await client.post(
            "/roles",
            headers=headers,
            json={"name": f"asg-{run_id}-{uuid.uuid4().hex[:5]}", "permissions": []},
        )
        role_id = data(created)["id"]
        responses = await _burst(
            client, "POST", f"/roles/users/{account.id}", headers=headers, json={"roleId": role_id}
        )
        listing = await client.get(f"/roles/users/{account.id}", headers=headers)
        await client.delete(f"/roles/{role_id}", headers=headers)

    assert _statuses(responses) == {204: PARALLEL}
    assert [r["id"] for r in data(listing)].count(role_id) == 1
