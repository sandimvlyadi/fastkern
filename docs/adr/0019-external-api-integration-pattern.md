# 19. External API integration pattern (port + httpx/tenacity adapter)

## Status
Accepted

## Context
Until now, "calling an external system" had exactly one worked example:
`notifications/infrastructure/email_adapter.py`, used for a fire-and-forget Celery task (ADR
0011) — the caller never waits on the response shape, so it never had to answer questions like
"what does a synchronous proxy endpoint do with a 404?" or "how does pagination map onto an
external API's own paging?".

The `posts` module (`src/app/modules/posts/`) is the first module where a request-path
**CRUD** endpoint is backed entirely by an external API instead of a local table (JSONPlaceholder
`/posts`, as a deliberately minimal demo/reference integration). Building it surfaced several
decisions — retry policy, error-class mapping, auth-header injection, whether a local DB
table is needed at all — that were made ad hoc and are worth fixing as a standard *before* a
second such integration arrives and either copies the ad hoc choices inconsistently or
re-derives them from scratch.

This ADR records the **why/when**; the step-by-step **how** stays in
[`docs/module-guide.md`](../module-guide.md) ("an adapter (optional)" in the Infrastructure
section) so the two documents don't drift by duplicating each other.

## Decision

- **The domain layer owns a `Port`** (an `abc.ABC` in `domain/ports.py`) describing the
  capability in domain terms — method names and types the application layer cares about
  (`get_post(post_id) -> Post | None`), never HTTP concepts (status codes, headers, URLs). The
  application layer depends only on the port, exactly like it depends on a repository port.

- **Infrastructure owns one adapter per external capability**, built from `httpx.AsyncClient` +
  `tenacity`, with this fixed shape (see `email_adapter.py` and
  `posts/infrastructure/jsonplaceholder_adapter.py` as the two reference implementations):
  - A `_client()` async context manager: yields an injected client if the constructor received
    one (so tests can supply `httpx.MockTransport`), otherwise opens a short-lived
    `httpx.AsyncClient`. **Never cache a client on the adapter instance** — the adapter may be
    constructed once but used across different event loops (a Celery task runs its own
    `asyncio.run()`), and a cached connection pool from a different loop breaks silently.
  - A single retrying method (conventionally `_post`/`_request`) wrapped in
    `@tenacity.retry(retry=retry_if_exception_type(_TransientXError), stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=4), reraise=True)`. `_TransientXError` is
    a **private** exception local to the adapter module — it never crosses the port boundary.
  - **Two failure classes, decided once per adapter, never per call site:**
    - *Transient* (connection/timeout errors, `5xx`, `429`): raise the private
      `_TransientXError`, which tenacity retries up to 3 times with exponential backoff. If
      still failing after retries, map it to a domain exception subclassing
      `app.core.exceptions.ExternalServiceError` (502) at the public method boundary.
    - *Permanent* (a `4xx` that retrying cannot fix — most commonly `404`): map it directly to
      the specific domain result (`None` from the port method, or a dedicated `NotFoundError`
      subclass raised by the *use case*, not the adapter — see `PostNotFoundError` /
      `GetPostUseCase`). Never retry a permanent failure.
    - *Update (2026-10-24):* any other outcome — a `4xx` with no domain meaning, a body that is
      not the expected JSON or lacks a field — is also mapped to the `ExternalServiceError`
      subclass and logged, never left to surface as an unhandled 500.
  - **Only idempotent calls are retried on every transient failure** *(update, 2026-10-24)*. A
    non-idempotent call (`POST`) may already have taken effect even when its reply was lost, so
    it is retried only when the request provably never left (`ConnectError`, `ConnectTimeout`,
    `PoolTimeout`) or was refused with a `429`; a `5xx` or read timeout on a `POST` fails
    straight to the domain error. (An idempotency key, when the partner API supports one, is
    the alternative that makes `POST` safe to retry.)

- **Auth is always injected by the adapter, sourced from `Settings`.** Add a
  `<MODULE>_API_BASE_URL` / `<MODULE>_API_AUTH_HEADER` / `<MODULE>_API_AUTH_TOKEN` (`SecretStr`)
  triplet (see `EMAIL_HTTP_*` and `POSTS_API_*` for the two existing examples) rather than
  hardcoding a header name or scheme. A safe, documented placeholder default
  (`dummy-posts-api-token`, clearly commented as non-secret) is fine for a demo/public API; a
  real partner integration MUST ship with no usable default and let
  `Settings._enforce_deploy_guards`-style production validation catch a forgotten credential —
  follow the `smtp`/`http` backend pattern in `config.py` if the integration is
  security-sensitive enough to warrant it.

- **Wire the adapter through `core/di.py`**, one `get_<module>_api_port(settings: Settings =
  Depends(get_settings)) -> XPort` provider per port, constructed fresh per request (same
  lifecycle as a repository provider) unless there's a specific, documented reason to reuse a
  connection pool across requests.

- **No local ORM model/repository/migration is required.** If the external API is the system
  of record for this data, skip `infrastructure/models.py` and `infrastructure/repository.py`
  entirely (delete the generator's stubs after running `scripts/new_module.py`) rather than
  inventing a shadow table nobody reads from. `tests/conftest.py`'s model-import step
  (module-guide.md §6) is then not needed either — there is no table to register.

- **This pattern is for request-path, synchronous calls** where a handler needs the external
  API's answer before it can respond (a CRUD proxy, a lookup, a validation call). It is
  **not** a replacement for ADR 0011's Celery dispatch pattern: fire-and-forget work (sending an
  email, pushing a webhook, anything the caller shouldn't block on) still goes through
  `celery_app.send_task(...)`, even if the task itself ends up using this same
  adapter-construction shape internally.

- **Testing**:
  - Unit-test the adapter against `httpx.MockTransport` (see `tests/unit/test_notifications.py`
    and `tests/unit/test_posts_adapter.py`) — assert the retry-then-raise path, the
    permanent-failure mapping, and that the configured auth header is actually sent. Never let a
    unit test hit the real external API.
  - Add an in-memory fake implementing the port to `tests/fakes.py` (e.g.
    `InMemoryPostsApiAdapter`) for use-case unit tests and for integration tests via
    `app.dependency_overrides[get_<module>_api_port] = lambda: fake`. Integration tests never
    call the real external API either — determinism and CI reliability outrank "test against
    the real thing" for a third-party dependency with no uptime SLA to this project.

## Consequences

- Future "call an external system" modules have one documented shape to copy (port in
  `domain/`, adapter in `infrastructure/`, `Settings` triplet, DI provider, `httpx.MockTransport`
  unit tests, fake-backed integration tests) instead of re-deriving retry/error-mapping/testing
  choices from scratch or copying `email_adapter.py` inconsistently.
- Retry/backoff tuning is a per-adapter decision (3 attempts, 0.5–4s exponential backoff is the
  default both existing adapters use) but is never silently inconsistent within one adapter —
  it lives in exactly one `@retry(...)` decorator per adapter.
- A consistency requirement (e.g. `UpdatePostUseCase` fetching before writing so an unknown id
  404s predictably instead of trusting an external API's write-endpoint status code) can cost an
  extra round trip. This is an accepted trade-off for correctness over raw latency; a provider
  that reliably 404s its own PUT/PATCH/DELETE on an unknown id can skip the pre-fetch.
- No new dependency was introduced — `httpx` and `tenacity` were already used by the
  `notifications` module.
