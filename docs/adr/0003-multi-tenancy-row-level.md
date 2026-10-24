# 3. Row-level multi-tenancy

## Status
Accepted

## Context
The base project needs to support multiple tenants. Options: separate
database per tenant, separate schema per tenant, or a shared schema with
a `tenant_id` column on every tenant-owned table (row-level).

## Decision
Row-level multi-tenancy: every tenant-owned table extends `StandardModel`
(`app/shared/infrastructure/orm_base.py`), which adds a
`tenant_id` column (a foreign key to `sys_tenants`; indexed through the table's
composite keys led by `tenant_id`, see ADR 0009). `BaseRepository` automatically scopes every query to
`get_request_context().tenant_id` unless explicitly opted out
(`tenant_scoped=False`, used only for platform-level tables like
`sys_tenants` itself).

## Consequences
- Lowest operational overhead (one database, one connection pool, one set
  of migrations) — appropriate for a base project where the number of
  tenants and their isolation/compliance requirements aren't yet known.
- Isolation is enforced in application code (repository scoping), not by
  the database engine itself. This is a deliberate trade-off for this
  stage; if a concrete future requirement needs stronger isolation
  (e.g. a regulated tenant that must not share infrastructure), migrate
  that tenant to schema- or database-per-tenant and record a new ADR —
  don't silently weaken the row-level scoping for everyone else.
- Every new tenant-owned table MUST extend `StandardModel` (or compose
  `TenantMixin` directly) — a table that forgets `tenant_id` is a data
  leak across tenants.
- **The tenants module is a platform-only surface.** Its repository is deliberately not
  tenant-scoped, so whoever may call `/tenants` acts on *every* tenant. `tenants:manage`
  (list/create/update) and `tenants:delete` (suspend/restore/delete) are therefore held only by
  the `*` roles (`developer`, `superadmin`) — never by a tenant `admin`. New tenants are
  provisioned with baseline `admin`/`user` roles; see
  [ADR 0012](0012-tenant-provisioning.md), including a known gap in bootstrapping their first
  admin.
