"""Timing sanity check for unknown-email logins, through the real endpoint.

Deliberately loose (a factor, not a millisecond figure) so it can't flake on a busy CI box:
before the fix an unknown email answered in ~3 ms against ~60 ms for a wrong password, a
ratio of ~0.05, far below the 0.3 asserted here."""

import statistics
import time

from httpx import AsyncClient

from tests.helpers import register_and_login

LOGIN = "/api/v1/auth/login"
ROUNDS = 5


async def _median_login_seconds(client: AsyncClient, payload: dict[str, str]) -> float:
    samples = []
    for _ in range(ROUNDS):
        started = time.perf_counter()
        response = await client.post(LOGIN, json=payload)
        samples.append(time.perf_counter() - started)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_credentials"
    return statistics.median(samples)


async def test_unknown_email_takes_comparable_time_to_a_wrong_password(
    client: AsyncClient, tenant_id: str
) -> None:
    await register_and_login(client, tenant_id, "timing@example.com")

    unknown = await _median_login_seconds(
        client, {"tenantId": tenant_id, "email": "nobody@example.com", "password": "Wr0ng-guess!"}
    )
    wrong = await _median_login_seconds(
        client, {"tenantId": tenant_id, "email": "timing@example.com", "password": "Wr0ng-guess!"}
    )

    assert unknown >= 0.3 * wrong, f"unknown={unknown:.4f}s wrong={wrong:.4f}s"
