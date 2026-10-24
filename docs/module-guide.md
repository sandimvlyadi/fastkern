# Adding a new module

Run the generator first:

```bash
python scripts/new_module.py orders
```

This creates `src/app/modules/orders/{domain,application,infrastructure,presentation}/__init__.py`
and adds `"app.modules.orders"` to the `containers` list of the `layers`
contract in `pyproject.toml`'s `[tool.importlinter]` section, so the
Clean Architecture layering rule is enforced for it from the start.

Then fill it in, in this order (each step only depends on the previous
one — you can commit after each):

## 1. Domain (`domain/`)

- `entities.py`: a `@dataclass(kw_only=True)` extending
  `app.shared.domain.entity.TenantScopedEntity` (or `AuditedEntity`/
  `Entity` for non-tenant-owned concepts). Put behavior here (e.g.
  `order.cancel()`), not just data.
- `exceptions.py`: subclass `app.core.exceptions.{NotFoundError,
  AlreadyExistsError,ConflictError,ForbiddenError,...}` — give each a
  unique `code` (used in the API error envelope), a default `message`
  (English fallback), and a `key` (translation lookup passed to
  `app.core.i18n.translate()`). Add a matching entry for that `key` to
  **both** `app/locales/en.json` and `id.json` — this is mandatory, not
  optional; `tests/unit/test_i18n_key_coverage.py` fails CI if you skip
  it. See `app/modules/users/domain/exceptions.py` for the pattern.
- `ports.py`: an `abc.ABC` with the methods your use cases need
  (`get_by_id`, `add`, `paginate`, ...). This is what infrastructure will
  implement and what application depends on — never the concrete
  repository.

## 2. Application (`application/`)

- `dto.py`: plain `@dataclass(frozen=True, slots=True)` inputs for your
  use cases (not Pydantic — that's a presentation-layer concern).
- `use_cases.py`: one class per use case, constructor takes the port(s)
  it needs, `async def execute(...)`. Raise domain exceptions here for
  business-rule violations (e.g. "already exists").

## 3. Infrastructure (`infrastructure/`)

- `models.py`: a SQLAlchemy model extending
  `app.shared.infrastructure.orm_base.StandardModel` (tenant-owned) or
  `GlobalModel` (platform-level — rare, ask first). Follow the database
  standards in `docs/adr/0009-database-naming-and-column-standards.md`:
  `__tablename__` needs a `sys_`/`master_`/`trans_` prefix
  (`"trans_orders"`), every table and column needs a `comment=`, a
  child table's parent FK uses `sort_order=ORDER_PARENT`, unique keys on a
  soft-delete table use `soft_delete_unique_index()`, and a NOT NULL column
  with a Python default needs a `server_default`. Column order is handled by
  the mixins. `tests/unit/test_schema_standards.py` fails otherwise.
- `repository.py`: extends
  `app.shared.infrastructure.repository.BaseRepository[YourModel,
  YourEntity]` and implements your domain port, plus `_to_entity`/
  `_to_model`. Add any custom queries (`get_by_...`) starting from
  `self._base_query()` so tenant/soft-delete scoping stays intact.
  **Unique keys and races.** A use case that checks "is the name free?" and then inserts can lose
  a race: two requests both see it free, both insert, and the database rejects the second. Map
  that to the same domain error the check raises by declaring, on the repository,
  `unique_violations: ClassVar[UniqueViolationMap] = {"uq_trans_orders_tenant_number": lambda
  order: OrderNumberTakenError(number=order.number)}` (key = the constraint/index name from the
  model, value = builds the error from the entity being written). `BaseRepository.add`/`save` then
  turn the violation into a `409` instead of a `500`; constraints you don't list re-raise as-is.
  Note the session's transaction is rolled back when this happens, so treat it as failing the
  whole request. Where a duplicate should be a *success* (an idempotent "assign"), use a single
  `INSERT … ON CONFLICT DO NOTHING` instead, as `UserRoleRepository.assign` does. Test the race
  with parallel requests on Postgres (`tests/integration/test_concurrent_writes.py`).
- `tasks.py` (optional): Celery tasks — add the module path to
  `include=[...]` in `app/core/celery_app.py`.
- an adapter (optional): if the module calls an external system, an
  `abc.ABC` port in `domain/ports.py` + an `httpx`+`tenacity` adapter
  here, following `app/modules/notifications/infrastructure/email_adapter.py`.

## 4. Presentation (`presentation/`)

- `schemas.py`: request/response models extending
  `app.shared.application.dto.CamelModel`. Give each one a
  `model_config = ConfigDict(json_schema_extra={"examples": [...]})` with
  one realistic example (real-looking values, not `"string"`/`0`) — see
  `app/modules/users/presentation/schemas.py::UserResponse` for the
  pattern — so `/docs`/`/redoc` show something a frontend dev can actually
  read (CLAUDE.md section 3).
- `router.py`: `APIRouter(prefix="/orders", tags=["orders"])`. Guard with
  `Depends(require_permission("orders:read"))` /
  `Depends(require_role(...))` from `app.core.di`. Every handler:
  parse request -> instantiate repository -> call use case -> map to a
  response schema -> `return success_envelope(...)`. For mutations, call
  `app.core.di.record_audit_event(...)` after the use case succeeds, passing
  `before=`/`after=` snapshots of the changed resource (build them with the
  entity's `audit_snapshot()`, never by dumping the whole entity — see
  `docs/adr/0006-audit-log-snapshots-and-pii.md`; a use case that can be a
  no-op, like assigning an already-held role, should return `None` so the
  router skips the event). Error
  translation is automatic — raise your domain exception (with its `key`,
  see step 1) and `app/core/errors.py` translates it; never call
  `translate()` yourself here. If the module dispatches a Celery task that
  produces user-facing text (e.g. a notification), pass `locale` as an
  explicit task argument, following
  `app/modules/notifications/application/use_cases.py` — a task has no
  ambient request context to read a locale from. Response datetimes stay
  UTC; do not call `to_user_timezone()` in a response schema (see
  CLAUDE.md section 3 and `docs/adr/0005-i18n-timezone-response-conventions.md`).
  Every request/response schema needs
  `model_config = ConfigDict(json_schema_extra={"examples": [{...}]})` with one realistic value
  — `tests/unit/test_schema_examples_coverage.py` discovers `presentation/schemas.py` files and
  fails otherwise. Use an obvious placeholder for anything secret-shaped
  (`"<refresh-token-returned-by-login>"`), never a JWT-looking string: gitleaks fails the commit.
  Validate what a client may set (permission strings, `locale`, `timezone`, pagination) in the
  schema, and never echo the rejected value back in the message.
  Also set `response_model=SuccessEnvelope[YourResponseSchema]` (or
  `SuccessEnvelope[list[YourResponseSchema]]` for a list endpoint), from
  `app.core.response`, on every non-204 route, and
  `responses=error_responses(YourDomainError, ..., validation=True)`, from
  `app.core.errors`, with the exact `AppError` subclasses this endpoint's
  dependencies and use case can actually raise — trace them from the code,
  don't guess (any existing router shows the pattern). Both are
  documentation-only: the handler still returns/raises the same plain
  dicts/exceptions as always; this only makes `/docs`/`/redoc` show the
  real success/error envelope shapes instead of FastAPI's generic
  placeholders (CLAUDE.md section 3).
  Rate limiting: the router's `dependencies=[...]` (from
  `app.core.rate_limit`) is `rate_limit(RateLimitTier.IP)`, then
  `get_current_user`, then `rate_limit(RateLimitTier.DEFAULT)`, then any
  `require_permission(...)` — in that order (the generated skeleton already
  does the first three). Add `RateLimitTier.WRITE` on mutating endpoints and
  include `RateLimitedError` in each endpoint's `error_responses(...)` —
  `tests/unit/test_rate_limit_coverage.py` fails otherwise (CLAUDE.md
  section 3, `docs/adr/0007-rate-limiting.md`).
- Register the router in `src/app/main.py`
  (`app.include_router(orders_router, prefix=api_prefix)`).

## 5. Migration

```bash
make migration m="add orders table"
make migrate
```

`make migration` assigns the next sequential revision id automatically
(`0001`, `0002`, ...) — see
`docs/adr/0013-migration-revision-naming.md`. Never pass `--rev-id`
by hand.

Remember to import your new `models.py` in `migrations/env.py` (the
autogenerate diff only sees models that have been imported). Autogenerate does
not know about the `updated_at` trigger: after `op.create_table(...)` for a
table with `updated_at`, add
`for s in updated_at_trigger_ddl(("trans_orders",)): op.execute(s)` (from
`app.shared.infrastructure.pg_triggers`) — see `migrations/versions/0001_baseline_schema.py`.

## 6. Tests

- `tests/unit/test_orders_entities.py` — domain behavior, no I/O.
- `tests/integration/test_orders_api.py` — via the `client` fixture,
  following `tests/integration/test_auth_flow.py`.
- `tests/conftest.py`: add `from app.modules.orders.infrastructure import models as
  _orders_models  # noqa: F401` next to the other modules' model imports. This is easy to
  miss because nothing points you at it: the `_create_schema` fixture's `Base.metadata.
  create_all()` only creates tables for modules that have been imported *somewhere*, and
  every existing module is hand-imported here. Skip this and every integration test for your
  new module fails with `no such table: <your_table>` (SQLite) / `UndefinedTable` (Postgres)
  — not an import error, so it's easy to misdiagnose as a migration problem.

## 7. Verify

```bash
make lint && make typecheck && make arch-check && make test
```

---

## Reference: the `files` module

A worked example of a module that is more than CRUD: a resumable upload lifecycle, an image pipeline
run by Celery, and storage behind a port. Decisions are recorded in
[ADR 0014](adr/0014-file-storage-and-resumable-uploads.md) (storage, uploads) and
[ADR 0015](adr/0015-image-pipeline-and-public-file-serving.md) (image pipeline, public serving).

### Layout

| Layer | What lives there |
|---|---|
| `domain/` | `state.py` (upload and file state machines), `signatures.py` (magic-byte detection, deny-list), `value_objects.py` (`StorageKey`, which only accepts server-made path parts), `ports.py` (`ObjectStoragePort`, `MalwareScannerPort`, `ImageProcessorPort`, repository ports) |
| `application/` | `use_cases.py` (one class per upload or file operation, plus `CleanupFilesUseCase`), `pipeline.py` (`ProcessUploadUseCase`: assemble, detect, scan, render, publish) |
| `infrastructure/` | `storage/local_fs.py` (the only code that maps keys to disk), `scanner.py` (no-op and ClamAV), `image/pillow_processor.py`, `factory.py` (chooses adapters from settings), `tasks.py` (Celery entry points), `repository.py`, `models.py` |
| `presentation/` | `router.py` (`/files/...` and `/public/files/...`), `schemas.py` |

### Rules to keep when changing it

- **Paths are built only from server-generated parts** (UUIDs and fixed names). Never put the client
  filename in a path; `display_name()` is for metadata only.
- **Size is enforced on bytes read, not on headers.** `Content-Length` is a fast-reject hint only.
- **Content decides the type**, never the client's Content-Type or extension.
- **Status changes are conditional UPDATEs** (`WHERE status = :expected`). A re-delivered task must
  not repeat or skip work.
- **Anti-IDOR:** a user who is not the owner and lacks `files:*` gets 404, not 403.
- **Public URLs** only serve PUBLIC, AVAILABLE variants, and only from the variant names stored in the
  database. Nothing is served from a directory listing.

### Flow

```
POST /files/uploads            → session INITIATED
PUT  .../chunks/{i}            → chunk stored (duplicate-safe), session UPLOADING
POST .../complete              → 202; file row UPLOADED; worker task files.process_upload dispatched once
worker: assemble → SCANNING (detect + scan) → PROCESSING (images only) → AVAILABLE
        or REJECTED (bad content) / FAILED (system problem)
files.cleanup (beat, every FILES_CLEANUP_INTERVAL_SECONDS): expired sessions, leftover dirs, failed artifacts
```

### Configuration

Settings live in `core/config.py` and are documented in `.env.example`. The ones most likely to need
tuning: `UPLOAD_CHUNK_MAX_BYTES` (per request), `UPLOAD_MAX_FILE_BYTES` (per file),
`MALWARE_SCAN_BACKEND` (`none` by default; `clamav` needs the `clamav` compose profile), and
`FILES_CLEANUP_INTERVAL_SECONDS` (900 for deployments; a small value only for local testing).

### Adding a new permission or scope

A new `require_permission("<resource>:<action>")` must also be added to
`roles.domain.permissions.PERMISSION_CATALOG`, and a new partner `require_scope(...)` to
`api_clients.domain.scopes.API_KEY_SCOPE_CATALOG`. Until it is, creating a role (or an API key) with it
is rejected, and `tests/unit/test_permission_catalog.py` fails `make test`. The catalogs are what
`GET /roles/permissions` and `GET /api-clients/scopes` serve to clients (ADR 0021).

### Adding a permission to an existing tenant

Tenants created before a change keep their old role definitions: the seed and tenant provisioning
create roles, but they never rewrite existing ones. When a module adds permissions to a baseline role,
ship a migration that grants them to existing tenant roles (the `files` permissions were added to the
seed and provisioning only, because this project had no production tenants yet).

### Running it locally

```bash
docker compose up -d --build        # api, worker, beat, db, redis, mailpit
make docker-seed                    # builds the seed image and seeds the tenant and roles
```

Then use the admin account from `.env` (`SEED_DEFAULT_PASSWORD`) to upload. The worker log shows
`file_processed`, and the beat log shows `Sending due task files-cleanup` once per interval.
