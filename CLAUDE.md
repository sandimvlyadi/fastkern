# CLAUDE.md — rules for AI agents working in this repository

This file governs how an AI coding agent (Claude Code or otherwise) must
work in this codebase. It is not a suggestion — the rules marked **MUST**
are enforced by tooling (`make arch-check`, `make lint`, `make typecheck`,
CI) and a PR that violates them will fail CI. Read this fully before
making any change. If a request conflicts with this file, follow this
file and say so, rather than silently picking one.

## 1. Mental model

This is a **modular monolith**: one deployable process, internally split
into independent `app/modules/<name>/`, each following **Clean
Architecture** with four layers, dependencies pointing inward only:

```
presentation -> infrastructure -> application -> domain
```

- **domain**: entities (plain dataclasses), domain exceptions, ports
  (`abc.ABC` interfaces the application layer depends on). **MUST NOT**
  import FastAPI, SQLAlchemy, Redis, Celery, httpx, or any other module's
  `domain`/`application`/`infrastructure`/`presentation` package.
- **application**: use cases (one class per use case, `execute()` method).
  Orchestrates domain entities + ports. **MUST NOT** import FastAPI or
  SQLAlchemy. May import another module's `domain` ports (not its
  `infrastructure`).
- **infrastructure**: SQLAlchemy models/repositories implementing the
  domain ports, Celery tasks, external API adapters.
- **presentation**: FastAPI routers + Pydantic request/response schemas.
  Thin: parse request -> call a use case -> map result to a response
  schema -> return `success_envelope(...)`. No business logic here.

Before writing code, identify which layer it belongs in. If you're
unsure, it's almost always **application** (a new use case) or
**infrastructure** (a new adapter) — not `presentation`, and never a new
top-level helper in `core/` unless it's genuinely cross-module.

## 2. Hard rules (enforced by `make arch-check` / CI)

1. **A module's `domain` layer imports nothing framework-specific.** If
   you find yourself importing `sqlalchemy` or `fastapi` in a
   `domain/` file, the code belongs in `infrastructure` or
   `presentation` instead.
2. **Never import another module's `infrastructure` package directly**
   (e.g. `app.modules.roles.presentation` importing
   `app.modules.users.infrastructure.repository`). Cross-module
   dependencies go through that module's `domain` **ports**, wired
   together in `app/core/di.py` (the composition root — the one place
   allowed to import infrastructure from more than one module). If a
   router needs another module's concrete repository, add a provider
   function to `core/di.py` and depend on that, following the existing
   pattern (`get_user_repository`, `get_current_user`, `require_role`,
   `require_permission`).
3. **Layers only depend inward.** `application` never imports
   `presentation`; `domain` never imports `application`.
4. Run `make arch-check` after adding/moving files that touch module
   boundaries. It runs `import-linter` against the contracts in
   `pyproject.toml`'s `[tool.importlinter]` section — treat a failure as
   a design signal, not an obstacle to silence.

## 3. Conventions to follow (not auto-enforced — review carefully)

- **Never raise `HTTPException` outside `presentation/`.** Raise (or
  subclass) `app.core.exceptions.AppError` from domain/application code;
  `app/core/errors.py` is the *only* place that maps exceptions to HTTP
  status codes. Adding a new domain exception type? Register its status
  code in `_STATUS_BY_ERROR` in `errors.py`.
- **Every response goes through the standard envelope**
  (`app.core.response.success_envelope` / the registered error handlers).
  Never return a bare dict or ORM model from a router.
- **Every endpoint documents its real shape in OpenAPI, not just its
  runtime data.** Set `response_model=SuccessEnvelope[YourResponseSchema]`
  (`SuccessEnvelope[list[...]]` for a list endpoint), from
  `app.core.response`, on every non-204 route — the handler still returns
  the same plain dict from `success_envelope()`, this is purely so
  `/docs`/`/redoc` show the real shape instead of an untyped object. Pass
  `responses=error_responses(YourDomainError, ..., validation=True)`, from
  `app.core.errors`, with the exact `AppError` subclasses that endpoint's
  dependencies/use case can actually raise — trace them from the code,
  don't guess (see any `presentation/router.py` for the pattern) — so
  `/docs` error examples match what `app/core/errors.py` actually returns
  instead of FastAPI's generic placeholder. Give every request/response
  schema in `presentation/schemas.py` a `model_config =
  ConfigDict(json_schema_extra={"examples": [...]})` with one realistic
  example (see `app/modules/users/presentation/schemas.py::UserResponse`)
  — a schema without one is a bug, not a style choice, same tier as a
  missing i18n `key`. See `docs/module-guide.md` section 4 for the
  fill-in-the-blanks version of this when adding a new module.
- **Every list endpoint is paginated** — use `OffsetParams`/`OffsetPage`
  (or `CursorParams`/`CursorPage` for feeds/infinite-scroll) from
  `app.core.pagination`. Never return an unbounded list.
- **Every ORM model for a tenant-owned aggregate extends `StandardModel`**
  (`app/shared/infrastructure/orm_base.py`) — this is what gives you
  `id`, `created_at`/`updated_at`, `created_by`/`updated_by`, soft-delete
  (`deleted_at`), and `tenant_id` for free, and is what makes
  `BaseRepository`'s automatic soft-delete filtering and tenant scoping
  work. Use `GlobalModel` only for genuinely tenant-independent tables
  (there should be very few of these — ask before adding one).
- **Every table follows the database standards** in
  `docs/adr/0009-database-naming-and-column-standards.md`, enforced for every
  ORM table by `tests/unit/test_schema_standards.py` (it discovers new modules
  automatically):
  - **Name** = `sys_` (platform/identity/governance), `master_` (reference
    data) or `trans_` (business transactions) + plural `snake_case`
    (`trans_orders`). Never an unprefixed table.
  - **Column order** = `id`, `tenant_id`, parent/FK ids, main columns,
    `created_by`/`updated_by`/`deleted_by`, `created_at`/`updated_at`/`deleted_at`.
    Postgres cannot reorder columns later, so this must be right in the first
    migration. The mixins set it via `sort_order`; a child table's parent FK
    uses `sort_order=ORDER_PARENT` (see `sys_user_roles`). Don't hand-declare
    `id`/timestamps/actors in a model.
  - Every table and column has a `comment=`. Every NOT NULL column with a
    Python-side default also has a `server_default`.
  - Unique keys on a soft-delete table use `soft_delete_unique_index()`
    (partial, `WHERE deleted_at IS NULL`), never a plain `unique=True` /
    `UniqueConstraint`. `tenant_id` needs no bare index: it must lead a
    composite index/unique matching the real query. JSON columns use `JsonB`.
  - A migration that creates a table with `updated_at` also creates its
    trigger (`updated_at_trigger_ddl`, see `migrations/versions/0001_baseline_schema.py`);
    CI's `scripts/check_updated_at_triggers.py` fails the build if it is missing.
- **Every migration's Alembic `revision` id is a sequential 4-digit
  number** (`0001`, `0002`, ...), never Alembic's default random hex —
  `make migration` assigns the next one automatically via
  `scripts/next_migration_id.py`. Enforced by
  `tests/unit/test_migration_naming.py` (fails `make test`/CI on a
  gap, a duplicate, or more than one Alembic head). See
  `docs/adr/0013-migration-revision-naming.md`.
- **Never hard-delete a tenant-owned row from application code.** Call
  `repository.delete(id)` (soft delete by default); only pass `hard=True`
  for genuine GDPR/compliance erasure flows, and note why in the commit.
- **Every repository query must go through `BaseRepository`'s
  `_base_query()`/`get_by_id()`/`paginate()`**, not a hand-rolled
  `select()` that skips tenant/soft-delete scoping. If you need a custom
  query (e.g. `get_by_email`), start from `self._base_query()` and add
  your `.where(...)`.
- **Every mutating endpoint that changes tenant/user-visible state calls
  `app.core.di.record_audit_event(...)`** after the use case succeeds —
  see `app/modules/users/presentation/router.py` (`update_user`,
  `deactivate_user`, `delete_user`) for the pattern. Skipping this for a
  new mutation is a bug, not a style choice.
- **RBAC**: guard a router/endpoint with `Depends(require_permission("<resource>:<action>"))`
  (checks all of a user's assigned roles) or `Depends(require_role("admin", ...))`
  (checks only the *active* role selected via `/auth/switch-role`) from
  `app.core.di`. Prefer `require_permission` unless the endpoint's
  behavior genuinely depends on which single role is currently active.
- **Every API endpoint is rate limited, in a fixed order.** A router's
  `dependencies=[...]` (from `app.core.rate_limit`) is, in this order:
  `Depends(rate_limit(RateLimitTier.IP))` FIRST (so requests that auth
  rejects still count), then `Depends(get_current_user)`, then
  `Depends(rate_limit(RateLimitTier.DEFAULT))` (keys on tenant + user),
  then any `require_permission(...)`/`require_role(...)` (so a 403 counts
  too) — see `app/modules/tenants/presentation/router.py`. Add
  `Depends(rate_limit(RateLimitTier.WRITE))` on POST/PATCH/PUT/DELETE
  endpoints, or `RateLimitTier.AUTH` for pre-login endpoints (see
  `app/modules/auth/presentation/router.py`), and include `RateLimitedError`
  in that endpoint's `error_responses(...)`. `/healthz`, `/readyz`,
  `/metrics` and the docs routes are deliberately never limited (probes and
  scrapes must not get a 429) — don't add the dependency to them. This is
  enforced by `tests/unit/test_rate_limit_coverage.py`; rationale in
  `docs/adr/0007-rate-limiting.md`.
- **i18n is mandatory for every user-facing string — error messages and
  notification content — enforced through one choke point, not scattered
  `translate()` calls.** Every `app.core.exceptions.AppError` subclass
  (and every module's `domain/exceptions.py` subclass of it) MUST set a
  `key: str` class attribute, with a matching entry in **both**
  `app/locales/en.json` **and** `app/locales/id.json`. Adding a new
  exception without a `key`, or without a locale entry for it, is a bug,
  not a style choice — see `app/modules/users/domain/exceptions.py` for
  the pattern to copy. `app/core/errors.py` is the *only* place that calls
  `app.core.i18n.translate(exc.key, locale, default=exc.message,
  **exc.context)` — routers and use cases never call `translate()`
  themselves for an error message, they just raise the `AppError` and let
  it bubble to the central handler (same shape as `record_audit_event`
  being the one choke point for audit events). The one documented
  exception: a Celery task has no ambient `RequestContext` (see
  `app/modules/notifications/application/use_cases.py`), so a task that
  produces user-facing text must receive `locale` as an explicit argument
  (sourced from the recipient's saved preference, not the caller's
  `Accept-Language`) and call `translate()` directly. This is
  automatically enforced: `tests/unit/test_i18n_key_coverage.py` fails
  `make test` if any `AppError` subclass is missing a `key` or a locale
  file is missing an entry for one. Internal log messages/exceptions stay
  English-only and don't need translation, and so is a raw
  `StarletteHTTPException.detail` (arbitrary developer text, not a
  catalog string — don't route it through `translate()`).
  `success_envelope()` stays data-only (no translated message field) —
  see `docs/adr/0005-i18n-timezone-response-conventions.md` for why.
- **Timezone is mandatory in the sense of *safety*, not per-response
  conversion.** Store and pass datetimes in UTC everywhere (DB, JSON) —
  every `StandardModel`/`GlobalModel` timestamp column uses
  `DateTime(timezone=True)` so this is structurally enforced, not just
  convention (see `app/shared/infrastructure/orm_base.py`). Never convert
  a response DTO's datetime field to the requester's local timezone — the
  client localizes using its own device timezone, same as any REST API.
  Only call `app.core.timezone.to_user_timezone()` at a genuine
  human-rendering edge (PDF/email/CSV) — never in a repository, use case,
  or presentation-layer response schema.
- **DTOs over the wire use `CamelModel`** (`app.shared.application.dto`)
  so JSON is camelCase while Python stays snake_case. Don't hand-roll
  `alias=` on every field.
- **Calling an external HTTP API**: implement a domain `Port` + an
  `infrastructure` adapter using `httpx` + `tenacity` retry for
  transient failures, following
  `app/modules/notifications/infrastructure/email_adapter.py`. Never
  call `httpx` directly from `application`/`presentation`.
- **Background work**: if an operation is slow, unreliable, or doesn't
  need to block the response (emails, exports, webhooks), dispatch a
  Celery task from `presentation` by task name with
  `await app.core.celery_app.dispatch_task("module.task_name", **kwargs)`,
  which also keeps modules decoupled the same way they'd be decoupled across
  a process boundary. Never call `celery_app.send_task(...)` or a task's
  `.delay()` from async code: publishing is blocking I/O, and during a broker
  outage it stalls the event loop for every request. `dispatch_task` runs it
  in a thread, and a broker outage becomes a 503 that rolls the request back.
  If enqueueing is only telemetry (not part of the request's outcome), catch
  and log the failure instead, as `ApiTrafficLogMiddleware` does.
- **Comments and docstrings never reference an ADR, this file, or an internal
  plan** (`ADR 0020`, `see docs/adr/...`, `CLAUDE.md section 3`, a decision code
  such as `(D3)`) — state the reason itself, in general terms, so the code
  makes sense on its own to someone who has only the repository. This covers
  everything that isn't documentation: `src/`, `tests/`, `scripts/`,
  `migrations/`, `.env.example`, and build/CI config. Docs (`README.md`, this
  file, `docs/`) may link ADRs, and an ADR may name code. Enforced by the
  `no-adr-references` and `no-claude-md-references` pre-commit hooks (so
  `make precommit` and CI fail on one). The rest of the comment style (explain
  why, say it once, no history) is in `CONTRIBUTING.md`.

## 4. Testing expectations

- New domain/application logic gets a **unit test** in `tests/unit/`
  (no DB, no HTTP client) — see `tests/unit/test_domain_entities.py`.
- New endpoints get an **integration test** in `tests/integration/`
  using the `client`/`session_factory` fixtures from `tests/conftest.py`
  — see `tests/integration/test_auth_flow.py` for the pattern (register
  -> login -> call the endpoint; use `session_factory` to seed
  roles/permissions directly when a test needs a role the API can't
  self-assign).
- **Tests must pass on Postgres, not just SQLite** — CI's `test-postgres` job runs the
  whole suite there. SQLite enforces neither foreign keys nor column lengths, so: a test
  that inserts a tenant-owned row needs a *real* tenant (and a real user for
  `sys_user_roles`) — use the `create_db_tenant` / `create_db_user` fixtures from
  `tests/conftest.py`, never a made-up `uuid4()` for a row that gets persisted. A Celery
  task that touches the DB must not use the shared engine in `app.core.db` (its asyncpg
  pool is bound to one event loop, but each task runs under its own `asyncio.run()`) —
  see `audit_log/infrastructure/tasks.py`. To run the suite on Postgres locally, see
  `docs/testing.md`.
- **`make test` needs a reachable Redis before it will pass**, even though it defaults to
  SQLite for the database. Redis backs the token session store and is the Celery broker, so
  any integration test that logs in, refreshes, or switches role (e.g.
  `tests/integration/test_auth_flow.py`) fails with a connection error — not a 500 from the
  app, a raw connection failure — if nothing is listening on `redis_url`
  (`redis://localhost:6379/0` by default). Start the local stack first:
  `docker compose up -d db redis mailpit` (db/mailpit aren't required by `make test` itself,
  but are needed for the Postgres suite / e2e tests you'll likely also run in the same
  session — see `docs/testing.md`). Run `docker compose ps` to confirm `db`, `redis`, and
  `mailpit` all report healthy before trusting any `make test` failure as a real bug —
  this bit a background agent during initial development (2026-10-01): a batch of
  integration-test failures were entirely explained by Redis not running yet, not by the
  code under test.
- A PR is not done until `make lint`, `make typecheck`, `make arch-check`,
  and `make test` all pass locally — run all four, not just the one you
  think is relevant.
- `make install` wires the first three of those (plus formatting and
  hygiene/secret-scanning checks) into a git pre-commit hook — never bypass
  it with `git commit --no-verify`. It doesn't run `make test`, so that one
  still needs a separate, explicit run.
- Before calling a change done, also run `make precommit` (`pre-commit run
  --all-files`) — it catches things `make lint`/`typecheck`/`arch-check`
  don't, most importantly `gitleaks` secret-scanning, which runs on every
  commit regardless of whether you remembered to invoke it yourself. This
  applies to example/placeholder content too, not just real code: a made-up
  OpenAPI/docstring example that happens to be JWT-shaped (`eyJ...` with
  two dots) or otherwise matches a secret pattern will still fail the hook
  and block the user's `git commit`, even though nothing sensitive was
  ever real. Prefer an obviously-fake placeholder (e.g.
  `<access-token-returned-by-this-endpoint>`) over a realistic-looking
  fabricated token/key in any example value.

## 5. Adding a new module

Run `python scripts/new_module.py <name>` — do not hand-create the folder
structure; the generator keeps every module's skeleton identical (and
registers it with `import-linter`). Then read
[`docs/module-guide.md`](./docs/module-guide.md) for the fill-in-the-blanks
walkthrough (entity -> port -> use case -> model -> repository -> router).

## 6. Things that look like a good idea but aren't (here)

- Don't add a generic/abstract repository method "just in case" before
  two real call sites need it.
- Don't reach for CQRS/event-sourcing for a straightforward CRUD module —
  this project is deliberately NOT that; open an ADR
  (`docs/adr/`) first if you think a module genuinely needs it.
- Don't add a new top-level dependency (new package in `pyproject.toml`)
  without checking `docs/adr/` for whether an equivalent choice was
  already made and rejected.
- Don't bypass `require_permission`/`require_role` with a manual
  `if user.email == "admin@..."` check — extend the RBAC model instead.
- Don't catch `AppError` (or its subclasses) in a router just to
  re-raise `HTTPException` — let it propagate to the global handler in
  `app/core/errors.py`.

## 7. Recorded decisions

Before changing anything architectural (layering, the composition-root
pattern in `core/di.py`, multi-tenancy strategy, auth strategy), read
[`docs/adr/`](./docs/adr/). If you make a new architectural decision,
add a new ADR file (copy the format of an existing one) instead of only
describing it in a PR description.
