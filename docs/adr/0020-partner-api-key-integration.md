# 20. Partner API key integration (inbound B2B authentication, scopes, and traffic accounting)

## Status

Accepted

## Context

Every integration this codebase has built so far is **outbound**: this application calling an
external system (`posts` module, ADR 0019; the email adapter, ADR 0011). Nothing lets a
third-party system call *into* this application. The first real requirement for that is a
known, fixed set of B2B partners (not public self-service signup) consuming a limited slice of
the API under their own tenant.

Reusing the existing human-auth stack (`sys_users` + JWT + RBAC via `require_permission`/
`require_role`) for this does not fit:

- A partner integration is not a person. It has no password, no session, no "active role" to
  switch, and should not be representable in `/auth/login`.
- RBAC permissions (`users:write`, `audit_log:read`, ...) are deliberately broad grants meant
  for a human operating the product. A partner integration needs a narrower, purpose-built
  grant — e.g. "can read posts for this tenant" — that must not silently widen if the tenant's
  RBAC role set changes later.
- `sys_audit_logs.actor_id` currently means "the user who did this." Partner-driven mutations
  need to be attributable too, without conflating the two kinds of actor.
- Nothing in this codebase logs raw inbound traffic (method/path/status/latency) today —
  `sys_audit_logs` is a domain-event log, not a request log, and per ADR 0006 it has no
  retention policy yet, which is wrong for a table a partner integration can grow quickly.

This ADR fixes the shape before a second partner integration arrives and either copies ad hoc
choices inconsistently or re-derives them from scratch — the same motivation as ADR 0019.

## Decision

- **New module `api_clients`**, scaffolded via `scripts/new_module.py api_clients`, following
  the same four-layer shape as every other module. It owns a new principal type, `ApiKey`,
  that is structurally distinct from `User` — there is no shared base type, no shared table,
  and no code path that can confuse the two.

- **Authentication is a bearer API key, verified by hash, not JWT.** The caller sends
  `Authorization: ApiKey <plaintext>` (or `X-API-Key: <plaintext>`); the plaintext is shown to
  the admin creating the key exactly once and only its hash (`sha256`, consistent with this
  project having no existing password-hash dependency to reuse for a non-password secret) is
  ever persisted, in `sys_api_keys.key_hash`. A new `get_current_api_client` FastAPI dependency
  in `core/di.py`, structurally parallel to `get_current_user`, resolves the key, rejects it if
  revoked/expired, and populates a new `api_client_id` field on `RequestContext` (parallel to
  `user_id`) — every downstream tenant-scoping and audit mechanism that already reads
  `get_request_context()` keeps working unmodified.

- **Scopes are independent of RBAC ("option b" from the preceding discussion), not permissions
  reused from `sys_roles`.** `sys_api_keys.scopes` is a `JsonB` list of `"resource:action"`
  strings, mirroring the *string shape* `Role.permissions` already uses (including the `"*"`
  wildcard convention from `Role.has_permission`) but stored directly on the key — there is no
  role assignment, no tenant-wide role catalog entry, and changing a tenant's human RBAC roles
  can never change what a partner key can do. A new `require_scope(scope: str)` dependency,
  structurally parallel to `require_permission`, is the only way a router checks it.

- **Key lifecycle is create / rotate / revoke, not create / delete:**
  - *Create*: returns the plaintext exactly once; only the hash and a short, non-secret
    `key_prefix` (for the admin UI to tell keys apart) are stored.
  - *Rotate*: issues a new key and keeps the old hash valid for a bounded overlap window
    (`previous_key_hash` / `previous_key_expires_at`, 24–72h, tenant-configurable) so a
    partner's own rotation is not a hard cutover.
  - *Revoke*: immediate invalidation, no overlap — for leaked-key incident response.
  - Management endpoints for all three are **human-operated**: they require an authenticated
    `User` via the existing `require_permission("api_clients:write")`, not the `ApiKey` itself.
    A partner cannot mint or rotate its own keys through the API; this avoids a chicken-and-egg
    bootstrap problem and keeps key issuance inside this tenant's existing RBAC-governed admin
    surface.

- **Audit events gain an `actor_type` alongside the existing `actor_id`.** `sys_audit_logs`
  gets a new `actor_type` column (`"user"` or `"api_client"`, `server_default='user'` so
  existing rows stay correctly attributed without a backfill). `core/di.record_audit_event`
  derives it from `RequestContext` at its one choke point — a partner-authenticated mutation
  needs no change at its call site, exactly like a human-authenticated one.

- **Raw partner request/response traffic gets its own table, `sys_api_request_logs`, separate
  from `sys_audit_logs`,** populated only for routes guarded by `get_current_api_client`.
  This is deliberately NOT the audit log: it is observability/billing/dispute-resolution data
  (method, path, status code, latency, response size, `api_key_id`), not a domain event, and
  it has a **30–90 day retention window enforced by a Celery beat purge task from day one** —
  unlike `sys_audit_logs`, which per ADR 0006 has no retention policy yet. The two tables must
  not be merged or given the same retention, or this table inherits "keep forever," which is
  the wrong default for a table one HTTP request grows, versus one domain mutation.
  Population happens via an ASGI middleware (parallel to `RequestContextMiddleware`) that fires
  a `celery_app.send_task(...)` after the response, the same fire-and-forget shape
  `audit_log.infrastructure.tasks.record_audit_event_task` already demonstrates — so a slow log
  write never adds latency to the partner's actual request.

- **A new rate-limit tier, `RateLimitTier.PARTNER`**, keyed on `api_key_id` rather than
  tenant+user, added to `app/core/rate_limit.py`'s `RateLimitTier` enum. Dependency order on a
  partner router is `IP` → `get_current_api_client` → `PARTNER` → `require_scope(...)`,
  structurally identical to the existing `IP → get_current_user → DEFAULT → require_permission`
  order `rate_limit.py`'s module docstring documents and `tests/unit/test_rate_limit_coverage.py`
  enforces for the user path. That structural test is extended (not forked) to also recognize
  the `get_current_api_client` / `PARTNER` pair, so a future partner route forgetting the tier
  or its order fails `make test` the same way a user route forgetting `DEFAULT` already does.

- **Partner-facing business endpoints live under their own router namespace**
  (`/api/v1/partners/...`), reusing existing use cases where one already exists rather than
  duplicating business logic — only the authentication/authorization/rate-limit dependency
  chain differs from the equivalent human-facing endpoint. This keeps the OpenAPI surface
  shown to partners small and intentional instead of growing implicitly every time an internal
  endpoint is added.

  *Update (2026-10-23):* the partner surface now covers read-only access to `posts`, `users`,
  `roles` (the role list and a user's role assignments), `master_examples`, `examples` and
  `files` (metadata and content, tenant-wide). Each resource checks its own `<resource>:read`
  scope at the route, so a key reaches only what it was explicitly granted. The audit log is
  deliberately **not** exposed to partners. `GET /roles/permissions` is not either: it answers
  "what may *this user* grant", which has no meaning for an API key.

- *Update (2026-10-24), from the post-v0.2.0 review:*
  - **Granting a scope requires holding the permission of the same name.** Scopes stay
    independent of RBAC at *check* time (`require_scope` never looks at roles), but *issuing* them
    is governed by RBAC: creating or rotating a key requires the caller to hold, across their
    roles, an RBAC permission named exactly like each scope on the key (`*` holders may grant
    anything) — the same `missing_grants` rule role editing uses. Without it, `api_clients:write`
    alone could mint a key that reads every user and file in the tenant. Rotation is covered
    because it hands the caller a working plaintext. `GET /api-clients/scopes` flags each entry
    `grantable` for the caller.
  - **`last_used_at` is written off the request path.** Verifying a key is read-only. An UPDATE
    during the request held the key's row lock until the request committed, so concurrent calls
    with one key queued behind each other. The `api_clients.record_request_log` task now also
    moves `last_used_at` forward, at most once a minute. It is therefore eventually consistent,
    and lost along with the log row if the broker is down.
  - **The request-log task is published after the response body is sent** (a Starlette
    background task on the response), not before the response is returned, so a slow or
    unreachable broker never delays a partner's call.
  - **The rotation overlap never outlives the key's own `expires_at`**, an expired key cannot be
    rotated (409 `api_key_already_expired`), and `expiresAt` must be timezone-aware and in the
    future.
  - **Keys carry a recognizable `pak_` marker** before the random part, and `.gitleaks.toml`
    detects it, so a leaked partner key is caught by secret scanning. `key_prefix` (shown to
    admins) is the marker plus 8 random characters. Keys issued before this keep working:
    verification is by hash only.

## Consequences

- A future partner-facing capability has one documented shape to copy (API key auth, scope
  check, partner rate-limit tier, request logging, audit actor attribution) instead of
  re-deriving authentication-for-machines from scratch or, worse, reusing human RBAC and
  having a partner's access silently change when someone edits a tenant's roles.
- `sys_audit_logs` and `sys_api_keys`/`sys_api_request_logs` are deliberately **not** merged
  into one "actor" abstraction spanning users and API clients beyond the new `actor_type`
  column — `ApiKey` and `User` remain structurally separate types with separate repositories,
  because an API key has no password, no session, no roles, and conflating them would pull
  RBAC concepts (active role, permission inheritance) onto a principal type that has neither.
- This is a second retention policy to operate (beyond the files-cleanup Celery beat task that
  already exists), not zero — the trade-off accepted here is an unbounded `sys_api_request_logs`
  table being a worse outcome than one more scheduled job.
- No new dependency is introduced — hashing uses the standard library, and the Celery
  fire-and-forget shape, `JsonB`, and `soft_delete_unique_index` are all already in use
  elsewhere in this codebase.
