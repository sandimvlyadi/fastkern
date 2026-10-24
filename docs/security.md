# Security model

## Authentication and sessions

Users log in with email and password (Argon2-hashed) and receive a short-lived JWT access token
and a refresh token. Every request re-checks the user, the tenant and the session on the server,
so a suspended tenant, a deactivated user or a logout takes effect before the token expires.

- **Refresh-token rotation with reuse detection:** presenting a refresh token that was already
  used revokes the whole session.
- **Logout revokes the session**, including its access tokens, not just the refresh token.
- **Login timing:** an unknown email costs the same Argon2 work as a wrong password, so response
  time doesn't reveal which emails are registered.

Redis holds the session state. If Redis is unavailable, login, refresh and logout return `503`
with `Retry-After`. The session denylist that authenticated requests consult fails open instead,
so a logged-out access token stays valid for at most its lifetime during an outage; watch
`session_denylist_backend_errors_total`. See [ADR 0010](./adr/0010-session-revocation.md).

## Multi-tenancy

Every tenant-owned table carries `tenant_id`, and every repository query is scoped to the caller's
tenant by `BaseRepository`. A query that would run without a tenant raises instead of returning
every tenant's rows. See [ADR 0003](./adr/0003-multi-tenancy-row-level.md).

The `/tenants` surface is platform-only: only the `developer` and `superadmin` roles hold
`tenants:manage` and `tenants:delete`. Creating a tenant provisions its baseline `admin` and `user`
roles. See [ADR 0012](./adr/0012-tenant-provisioning.md).

## RBAC

Users hold one or more roles; each role grants permissions named `resource:action`
(`users:read`, `files:write`, …).

- `require_permission("…")` passes if any of the user's roles grants the permission.
- `require_role("…")` checks only the *active* role, chosen at login or via `/auth/switch-role`.
- Permissions come from a catalog defined in code (`GET /roles/permissions`); a role can't be
  given a permission that no code checks.
- **No escalation:** a user can only grant permissions, or assign roles, that they hold
  themselves.

See [ADR 0004](./adr/0004-jwt-rbac-role-switching.md) and
[ADR 0021](./adr/0021-permission-and-scope-catalog-in-code.md).

## Partner API keys

Business partners call a separate, read-only API under `/api/v1/partners` with a long-lived key
in the `X-API-Key` header. A key is not a user: it has no password, session or role, only a tenant
and a list of scopes (`posts:read`, `users:read`, `files:read`, …) checked per route.

- Keys are issued, rotated and revoked by people with `api_clients:read` / `api_clients:write`,
  under `/api/v1/api-clients`. The plaintext is shown once; only a SHA-256 hash is stored.
- Issuing or rotating a key requires holding a permission named like each of its scopes, so key
  management can't be used to read more than the issuer can.
- Rotation keeps the previous key valid for an overlap window (`API_KEY_ROTATION_OVERLAP_HOURS`,
  default 48), never past the key's own expiry.
- Keys start with `pak_`, which the repository's gitleaks rule detects in a commit.
- Every partner request is logged to `sys_api_request_logs`, purged after
  `API_REQUEST_LOG_RETENTION_DAYS` (default 60).

See [ADR 0020](./adr/0020-partner-api-key-integration.md).

## Rate limiting

Every API endpoint is rate limited. The limit applied before authentication counts requests that
auth rejects too.

| Tier | Applies to | Keyed on | Setting |
|---|---|---|---|
| IP | every endpoint, before auth | client IP | `RATE_LIMIT_IP` |
| DEFAULT | every authenticated endpoint | tenant + user | `RATE_LIMIT_DEFAULT` |
| AUTH | register, login, refresh | client IP | `RATE_LIMIT_AUTH` |
| WRITE | POST, PATCH, PUT, DELETE | tenant + user | `RATE_LIMIT_WRITE` |
| UPLOAD | file chunk uploads | tenant + user | `RATE_LIMIT_UPLOAD` |
| PARTNER | partner endpoints | API key | `RATE_LIMIT_PARTNER` |

`/healthz`, `/readyz`, `/metrics` and the docs routes are never limited, so probes and scrapes
can't get a `429`. A rejected request returns `429` with `Retry-After`. `RATE_LIMIT_ENABLED=false`
turns limiting off, for a controlled load test.

If Redis is unavailable the limiter fails open and reports it through
`rate_limit_backend_degraded` and `rate_limit_backend_errors_total`.

### Behind a reverse proxy

Set `FORWARDED_ALLOW_IPS` to the proxy's address. Without it every client shares the proxy's IP
bucket. Never use `FORWARDED_ALLOW_IPS=*` on an internet-reachable port: a spoofed
`X-Forwarded-For` would let a client pick its own IP.

See [ADR 0007](./adr/0007-rate-limiting.md).

## Files

Uploaded files are classified by their content (magic bytes), never by the client's filename or
Content-Type. Executables and active markup are refused, and anything that isn't an image is
always served as a download. Images are re-encoded without EXIF, XMP or GPS metadata. Optional
ClamAV scanning (`MALWARE_SCAN_BACKEND=clamav`) runs before a file becomes available. A user sees
only their own files unless they hold the matching `files:*` permission; anyone else gets
`404`, so a file's existence doesn't leak.

See [ADR 0014](./adr/0014-file-storage-and-resumable-uploads.md) and
[ADR 0015](./adr/0015-image-pipeline-and-public-file-serving.md).

## Reporting a vulnerability

See [SECURITY.md](../SECURITY.md).
