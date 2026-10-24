# 21. Permission and scope catalogs are defined in code, not in the database

## Status

Accepted

## Context

A role's `permissions` (`sys_roles.permissions`) and an API key's `scopes`
(`sys_api_keys.scopes`, ADR 0020) can be edited through the API, but nothing told a client
which values exist. A frontend building a "pick the permissions for this role" screen had to
hard-code its own copy of the list, and the API accepted any well-formed string:

- A typo such as `user:read` was stored and silently granted nothing, because no code checks
  for it.
- `resource:*` matched `PERMISSION_PATTERN` but was never honored. `Role.has_permission`, like
  `ApiKey.has_scope`, only matches a literal string or the bare `*`, so `roles:*` granted
  nothing either.
- `CreateApiKeyRequest.scopes` had no validation at all: any strings, any count, `*` included.

There are two places such a catalog could live: a table (a `GlobalModel`, since the catalog
is not tenant-owned) seeded on deploy, or constants in the domain layer.

## Decision

- **Code-defined catalogs in the domain layer.** `roles.domain.permissions.PERMISSION_CATALOG`
  lists every RBAC permission. `api_clients.domain.scopes.API_KEY_SCOPE_CATALOG` lists every
  partner scope, kept separate per ADR 0020's "scopes are independent of RBAC". A permission
  only means something because a `require_permission(...)`/`require_scope(...)` (or an
  in-handler check such as `files`' `_has_permission`) tests for it, so the code is already the
  source of truth, and a deploy is what changes it anyway. A table would add a second copy to
  keep in sync (a seed step on every deploy) without adding integrity: `permissions` is a
  `JsonB` list, not a foreign key, and making it one is a schema rewrite with no payoff today.
  `roles.domain.baseline`, the existing single source of truth for baseline roles, already set
  this precedent.
- **A structural test keeps the catalogs honest.** `tests/unit/test_permission_catalog.py`
  reads the permission/scope out of every served route's dependency closures, plus in-handler
  `_has_permission(...)` calls. It fails `make test` if a checked value is missing from its
  catalog, or a catalog entry is checked nowhere. This follows the same pattern as
  `test_rate_limit_coverage.py`.
- **Read endpoints:**
  - `GET /roles/permissions` (`roles:read`) returns each entry with `resource`, `action`,
    `platformOnly` and a per-caller `grantable` flag. The flag uses the same `missing_grants`
    rule the escalation guard enforces. Platform-only entries (`*`, `tenants:*`) are hidden
    from a caller who cannot grant them.
  - `GET /api-clients/scopes` (`api_clients:read`) returns the scope catalog.
  - Both are paginated (`OffsetParams`/`OffsetPage`, via `core.pagination.paginate_sequence`)
    like every list endpoint, even though the catalogs are small and bounded.
  - Neither returns translated labels: the envelope stays data-only (ADR 0005), and the
    client maps `code` to its own label.
- **Writes are validated against the catalogs:**
  - Creating a role, or adding a permission to one, raises `UnknownPermissionError` (422) for
    anything outside the catalog. This applies even to a `*` caller. On update only *added*
    permissions are checked, so a stale entry already on a role never blocks an unrelated edit.
  - Creating an API key raises `UnknownScopeError` (422) likewise. Its schema now also caps
    the count, checks the `resource:action` format and drops duplicates.
  - `*` is not a scope: a partner key gets an explicit, narrow grant. `ApiKey.has_scope` still
    honors a `*` stored before this decision.
- **`resource:*` is no longer accepted** by `PERMISSION_PATTERN`. It never worked. Making it
  work would mean changing `has_permission`/`missing_grants` semantics, which nothing needs
  now that the client can list a resource's actions explicitly.

## Consequences

- Adding a guarded endpoint with a new permission or scope now means adding one line to the
  catalog. The structural test fails until that line is there.
- Permissions can't be switched on or off per tenant (or per plan) without a deploy. That
  requirement doesn't exist today. If it appears, a table becomes worth it, and that change
  needs its own ADR.
- Existing rows holding a now-unknown string (`resource:*`, a typo) keep it. It still grants
  nothing, it can be dropped through PATCH, and it is never re-validated on an unrelated edit.
