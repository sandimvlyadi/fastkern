# 9. Database naming and column standards

## Status
Accepted

## Context
A DBA review of the first schema found two things. Tables had no naming
convention (`users`, `roles`, `audit_logs`, ...), so nothing told an operator
whether a table was platform plumbing, reference data or business
transactions. And columns were in an accidental order — business columns
first, `id`, timestamps and `tenant_id` scattered after them — because
SQLAlchemy orders mixin columns after a subclass's own. Postgres cannot
reorder columns once a table exists (only a full table rebuild can), so the
order has to be right from the first migration.

The review also raised the standards an enterprise database normally has
and this one lacked: a `deleted_by` actor for soft deletes, a data
dictionary (comments), server-side defaults, no redundant indexes, and
unique keys that respect soft delete.

## Decision

### Table names
`snake_case`, plural, with a mandatory purpose prefix
(`^(sys|master|trans)_[a-z][a-z0-9_]*$`):

| Prefix | Holds | Examples |
|---|---|---|
| `sys_` | Platform, identity and governance: tenants, users, roles, grants, audit trail, settings, job state | `sys_tenants`, `sys_users`, `sys_roles`, `sys_user_roles`, `sys_audit_logs` |
| `master_` | Reference / master data: slow-changing, referenced by many transactions | `master_products`, `master_categories`, `master_currencies` |
| `trans_` | Business transactions and documents: grows with activity, usually append-heavy | `trans_orders`, `trans_order_items`, `trans_payments` |

A junction or child table takes the prefix of the domain it belongs to
(`sys_user_roles`, `trans_order_items`). When unsure: does it exist because
of the business (`master_`/`trans_`) or because of the platform (`sys_`)?
Is it a thing the business keeps (`master_`) or an event that happened
(`trans_`)?

### Column order
Fixed, enforced by `mapped_column(sort_order=...)` in the mixins
(`ORDER_*` constants in `orm_base.py`):

1. `id`
2. `tenant_id`
3. parent / foreign-key ids (`<singular>_id`) — child tables
4. main columns (a model's own columns; `sort_order` 0)
5. actors: `created_by`, `updated_by`, `deleted_by`
6. timestamps: `created_at`, `updated_at`, `deleted_at`

### Mandatory columns
`StandardModel` (tenant-owned) and `GlobalModel` (platform-wide) carry all of
the above. `deleted_at` implies `deleted_by`. Two documented exceptions,
both deliberate:

- **Append-only** tables (`sys_audit_logs`) have `id`, `tenant_id`, main
  columns, `created_at` only. `actor_id` there is the event's subject, not
  a row-maintenance column.
- **Pure junctions** (`sys_user_roles`) are inserted and hard-deleted, never
  edited: `id`, `tenant_id`, the two parent ids, `created_at`. Who granted
  or revoked is in the audit log.

Actor columns are filled by `BaseRepository` from the request context's
authenticated user: `add()` sets `created_by`/`updated_by` (an actor already
on the entity wins), `save()` sets `updated_by`, `delete()` sets `deleted_by` (and `updated_by`, because
the trigger bumps `updated_at` for the soft delete).
With no authenticated user (Celery task, seed script, pre-login flow such as
self-registration) they are left as they were — NULL on insert, unchanged on
update. **NULL means "no authenticated actor", and that is deliberate for
self-registration**: nothing reads these columns (not the API, not the audit
snapshots, not authorization), the `user.registered` audit event already
records the new user as its actor, and stamping "self" would add a
special case to `RegisterUseCase` for the persistence layer's concern. The
cost: `created_by` alone cannot tell a self-signup from a seeded or job-created
row — the audit log can. Pinned by
`test_self_registration_leaves_actor_columns_null`. This is done in the repository rather than with a column
`onupdate`, which would null `updated_by` on a job's edit. Raw SQL and bulk
updates bypass it by nature.

Actor columns are plain UUIDs **without FKs**: an FK to `sys_users` is
circular for `sys_users` itself and `sys_tenants`, and would block a GDPR
hard-delete of a user who once touched other rows.

### Keys, indexes, defaults, types
- Primary key: UUIDv7 (`app.shared.domain.identifiers.uuid7`), so inserts
  are right-append in the PK b-tree instead of random-page writes (UUIDv4).
  Same `uuid.UUID` type everywhere. Switch to the stdlib `uuid.uuid7()` when
  the minimum Python reaches 3.14.
- Every unique key on a soft-delete table is a *partial* unique index
  (`soft_delete_unique_index()`: `WHERE deleted_at IS NULL`), otherwise a
  deleted row reserves its value forever.
- `tenant_id` has no bare index. It must lead a composite index/unique that
  matches the real access path (`(tenant_id, email)`); a bare index next to
  it is write overhead. Every other FK column is indexed.
  Limitation to know: on a soft-delete table these composites are *partial*
  (`WHERE deleted_at IS NULL`), so Postgres uses them only for queries that
  also exclude deleted rows — which is every default `BaseRepository` query.
  A feature that lists tenant rows *including* deleted ones (a "trash" view,
  a purge job) must add its own plain index at that point; none exists today
  (nothing calls `include_deleted=True`), so none is added speculatively.
- Every NOT NULL column with an application-side default also has a
  `server_default`, so raw SQL / ETL inserts behave like ORM inserts.
- JSON columns are `JSONB` on Postgres (`JsonB` in `orm_base.py`; plain JSON
  on SQLite).
- Every table and every column has a `comment` — the data dictionary.
- `updated_at` is maintained by a Postgres trigger (`sys_set_updated_at`,
  `pg_triggers.py`) in addition to the ORM `onupdate`, so bulk `UPDATE`s,
  psql fixes and ETL cannot leave it stale. Migrations that create a table
  with `updated_at` must add its trigger (`updated_at_trigger_ddl`);
  `create_all()` (tests) does it via a metadata listener.
  The trigger **always** sets `updated_at = now()`, overriding any value
  written explicitly. A backfill or import that must preserve historical
  timestamps disables it for the load and re-enables it afterwards:
  `ALTER TABLE <t> DISABLE TRIGGER trg_<t>_set_updated_at` /
  `ENABLE TRIGGER ...`. `scripts/check_updated_at_triggers.py` counts a
  disabled trigger as missing, so a forgotten re-enable is caught.
- Naming: constraints/indexes follow `_NAMING_CONVENTION`
  (`pk_`, `fk_`, `uq_`, `ix_`, `ck_` + table name); booleans are
  `is_`/`has_`; datetimes are UTC `timestamptz`.
- CHECK constraints are declared on the model with a short `name=` that the naming convention
  expands to `ck_<table>_<name>` (so the migration spells the full name via `op.f(...)`).
  Currently one: `ck_sys_users_email_lower` (`email = lower(email)`) on `sys_users`. The unique
  index on `(tenant_id, email)` is case-sensitive, so without it `Jane@x.io` and `jane@x.io`
  were two accounts; the app lowercases on the way in (`normalize_email`) and the constraint
  makes that structural. It was chosen over a functional `lower(email)` unique index because it
  leaves `soft_delete_unique_index()` untouched. Repositories map a violated *unique* key to a
  domain error (`BaseRepository.unique_violations`, keyed by constraint name), so a race between
  two writers is a `409`, not a `500`.

### Enforcement
`tests/unit/test_schema_standards.py` runs every rule above against every
ORM table (models are discovered from disk, so a new module is covered
automatically) and also tests each rule against a deliberately bad table so a
rule cannot silently stop checking. CI's `test-postgres` job runs
`alembic upgrade head`, `alembic check` (models == migrations) and
`alembic downgrade base`, plus the trigger tests that only run on Postgres and
`scripts/check_updated_at_triggers.py`, which fails the job if any table with
`updated_at` lacks its trigger in the *migrated* schema (autogenerate cannot
see triggers, and the tests build their schema with `create_all()`).

### Migration policy
The three pre-release revisions had never been applied to any database, so
they were squashed into one baseline (`0001`) instead of being layered with
rename-and-rebuild migrations. From here on, migrations are append-only. A
future change of column order on an existing table means a table rebuild —
avoid it by getting the order right in the model first.

Until the **first deployment**, `0001` may still be edited in place (the
`ck_sys_users_email_lower` constraint was added that way, see
[ADR 0012](0012-tenant-provisioning.md)); a database created from an older `0001` does not
receive such an edit, so recreate it (`docker compose down -v`) and re-seed. The first
deployment freezes `0001`: every schema change after it is a new revision.

## Consequences
- Every new table is named, ordered, commented and indexed the same way;
  reviewers and DBAs can rely on it, and CI fails otherwise.
- Existing raw SQL, dashboards or scripts that reference the old names
  (`users`, `roles`, ...) must use the `sys_` names. No known consumers exist
  yet (nothing had been deployed).
- The 100+ char `comment=` strings add boilerplate to models; that is the
  price of an in-database data dictionary.

## Deferred, deliberately
- **`INET` for `sys_audit_logs.ip_address`.** The value is
  `request.client.host` as the ASGI server reports it: behind a proxy or a
  test client it is not always an IP (Starlette's test client reports
  `testclient`), and an `INET` column would reject the insert and lose the
  audit row. Revisit once client-IP resolution is normalised.
- **Partitioning / retention for `sys_audit_logs`.** Range-partition by
  `created_at` once volume warrants; the bare `created_at` index is there for
  retention purges until then.
- **Normalising `sys_roles.permissions`** (JSONB list) into a
  `sys_role_permissions` table, if permissions ever need to be queried
  ("which roles grant X?") at scale.
