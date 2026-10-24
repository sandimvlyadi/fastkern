"""End-to-end tests: real HTTP against a RUNNING, SEEDED docker-compose stack.

Everything here is excluded from the default `pytest` run (see `addopts` in pyproject.toml). Run:

    make docker-up && make docker-seed     # stack + seed data
    make e2e                               # general suite (rate limits lifted)
    make e2e-ratelimit                     # rate-limit tests (tiny limits, then restores)
    make e2e-destructive                   # stops/starts redis and db

Unlike the unit/integration suites these need no DB of their own: they use the seeded accounts
(`superadmin@example.com`, `admin@example.com`, `user@example.com`) and create everything else
under a per-run id, so they can be re-run against the same stack.

Settings: E2E_BASE_URL (default http://localhost:8000), then for SECRET_KEY / SEED_DEFAULT_PASSWORD
the value in E2E_<NAME>, the environment (SEED_* only), or the repo's `.env`.
"""

import itertools
import os
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest

E2E_DIR = Path(__file__).resolve().parent
ROOT = E2E_DIR.parents[1]


def _dotenv() -> dict[str, str]:
    path = ROOT / ".env"
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            match = re.match(r"^\s*([A-Z_][A-Z0-9_]*)=(\S*)", line)
            if match:
                values[match[1]] = match[2]
    return values


_DOTENV = _dotenv()


def setting(name: str, default: str) -> str:
    """`E2E_<NAME>`, then (SEED_* only) the plain env var, then `.env`, then `default`.

    The plain env is deliberately not consulted for the rest: pytest-env sets SECRET_KEY etc. for
    the unit suite, and those are not the running stack's values."""
    candidates = [os.environ.get(f"E2E_{name}")]
    if name.startswith("SEED_"):
        candidates.append(os.environ.get(name))
    candidates.append(_DOTENV.get(name))
    return next((value for value in candidates if value), default)


BASE_URL = os.environ.get("E2E_BASE_URL", "http://localhost:8000").rstrip("/")
API = f"{BASE_URL}/api/v1"
TENANT_ID = setting("DEFAULT_TENANT_ID", "00000000-0000-0000-0000-000000000000")
PASSWORD = setting("SEED_DEFAULT_PASSWORD", "ChangePlease123!")
SECRET_KEY = setting("SECRET_KEY", "")
STRONG_PASSWORD = "Str0ngPass!"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Everything under tests/e2e is an e2e test (no per-module `pytestmark` to forget)."""
    for item in items:
        if E2E_DIR in Path(str(item.fspath)).parents:
            item.add_marker(pytest.mark.e2e)


@pytest.fixture(scope="session", autouse=True)
def _create_schema() -> None:
    """Overrides the root conftest's fixture of the same name: e2e talks to a running stack and
    must not create (or drop!) tables in the unit-test database."""


@dataclass(frozen=True)
class Tokens:
    access: str
    refresh: str


@dataclass(frozen=True)
class Account:
    id: str
    email: str
    password: str
    tenant_id: str
    tokens: Tokens


class Api:
    """Thin wrapper over httpx: relative `/api/v1` paths and an optional bearer token."""

    def __init__(self) -> None:
        self.http = httpx.Client(base_url=API, timeout=30)

    def request(
        self, method: str, path: str, token: str | None = None, **kwargs: Any
    ) -> httpx.Response:
        headers = dict(kwargs.pop("headers", None) or {})
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return self.http.request(method, path, headers=headers, **kwargs)

    def get(self, path: str, token: str | None = None, **kwargs: Any) -> httpx.Response:
        return self.request("GET", path, token, **kwargs)

    def post(self, path: str, token: str | None = None, **kwargs: Any) -> httpx.Response:
        return self.request("POST", path, token, **kwargs)

    def patch(self, path: str, token: str | None = None, **kwargs: Any) -> httpx.Response:
        return self.request("PATCH", path, token, **kwargs)

    def delete(self, path: str, token: str | None = None, **kwargs: Any) -> httpx.Response:
        return self.request("DELETE", path, token, **kwargs)

    def login(
        self, email: str, password: str = PASSWORD, tenant_id: str = TENANT_ID, **kwargs: Any
    ) -> httpx.Response:
        return self.post(
            "/auth/login",
            json={"tenantId": tenant_id, "email": email, "password": password},
            **kwargs,
        )

    def tokens(self, email: str, password: str = PASSWORD, tenant_id: str = TENANT_ID) -> Tokens:
        response = self.login(email, password, tenant_id)
        assert response.status_code == 200, (
            f"login as {email} failed ({response.status_code}): {response.text[:200]}. "
            "Is the stack seeded (`make docker-seed`) with the same SEED_DEFAULT_PASSWORD?"
        )
        body = data(response)
        return Tokens(access=body["accessToken"], refresh=body["refreshToken"])


def data(response: httpx.Response) -> Any:
    return response.json()["data"]


def error_code(response: httpx.Response) -> str:
    return str(response.json()["error"]["code"])


def expect(response: httpx.Response, status: int, code: str | None = None) -> httpx.Response:
    """Assert status (and, for an error envelope, its `error.code`); returns the response."""
    assert response.status_code == status, (
        f"expected {status}, got {response.status_code}: {response.text[:400]}"
    )
    if code is not None:
        assert error_code(response) == code, response.text[:400]
    return response


@pytest.fixture(scope="session")
def api() -> Api:
    return Api()


@pytest.fixture(scope="session", autouse=True)
def _stack_is_up(api: Api) -> None:
    deadline = time.time() + 60
    while True:
        try:
            if httpx.get(f"{BASE_URL}/readyz", timeout=5).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        if time.time() > deadline:
            pytest.exit(
                f"No ready stack at {BASE_URL}/readyz. Start and seed it first: "
                "`make docker-up && make docker-seed`, then `make e2e`.",
                returncode=2,
            )
        time.sleep(2)


@pytest.fixture(scope="session")
def run_id() -> str:
    return uuid.uuid4().hex[:8]


@pytest.fixture(scope="session")
def superadmin(api: Api) -> Tokens:
    return api.tokens("superadmin@example.com")


@pytest.fixture(scope="session")
def admin(api: Api) -> Tokens:
    return api.tokens("admin@example.com")


@pytest.fixture(scope="session")
def plain_user(api: Api) -> Tokens:
    return api.tokens("user@example.com")


@pytest.fixture(scope="session")
def roles(api: Api, superadmin: Tokens) -> dict[str, str]:
    """Seeded role name -> id, in the default tenant."""
    response = expect(api.get("/roles", superadmin.access, params={"page_size": 200}), 200)
    return {role["name"]: role["id"] for role in data(response)}


@pytest.fixture
def make_user(api: Api, run_id: str) -> Callable[..., Account]:
    """Registers a fresh user (default role: `user`) and logs them in."""
    counter = itertools.count()

    def _make(
        *,
        prefix: str = "u",
        password: str = STRONG_PASSWORD,
        tenant_id: str = TENANT_ID,
        full_name: str = "E2E User",
    ) -> Account:
        email = f"{prefix}.{run_id}.{next(counter)}.{uuid.uuid4().hex[:4]}@example.com"
        response = expect(
            api.post(
                "/auth/register",
                json={
                    "tenantId": tenant_id,
                    "email": email,
                    "password": password,
                    "fullName": full_name,
                },
            ),
            201,
        )
        return Account(
            id=data(response)["id"],
            email=email,
            password=password,
            tenant_id=tenant_id,
            tokens=api.tokens(email, password, tenant_id),
        )

    return _make


@pytest.fixture
def make_admin(
    api: Api, make_user: Callable[..., Account], superadmin: Tokens, roles: dict[str, str]
) -> Callable[[], Account]:
    """A fresh user holding the seeded `admin` role (their own account, so tests may safely
    deactivate/delete *other* users with it, or be targeted themselves)."""

    def _make() -> Account:
        account = make_user(prefix="adm")
        expect(
            api.post(
                f"/roles/users/{account.id}", superadmin.access, json={"roleId": roles["admin"]}
            ),
            204,
        )
        # `admin` sorts before `user`, so a new login picks it as the active role.
        return Account(
            id=account.id,
            email=account.email,
            password=account.password,
            tenant_id=account.tenant_id,
            tokens=api.tokens(account.email, account.password, account.tenant_id),
        )

    return _make


def forge_access_token(user_id: str, **overrides: Any) -> str:
    """An access token signed with the stack's SECRET_KEY (from `.env`); `None` drops a claim."""
    if not SECRET_KEY:
        pytest.skip("SECRET_KEY unavailable (set E2E_SECRET_KEY or provide .env)")
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "sub": user_id,
        "type": "access",
        "jti": str(uuid.uuid4()),
        "iat": now,
        "exp": now + timedelta(minutes=5),
        "tenant_id": TENANT_ID,
        "active_role": "user",
    }
    claims.update(overrides)
    return jwt.encode(
        {key: value for key, value in claims.items() if value is not None},
        SECRET_KEY,
        algorithm="HS256",
    )
