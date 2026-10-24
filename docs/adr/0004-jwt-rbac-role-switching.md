# 4. Self-managed JWT auth with RBAC and role switching

## Status
Accepted

## Context
The base project needs authentication/authorization from day one, without
presupposing every future product will use the same identity provider.

## Decision
- Auth: self-managed access+refresh JWT pairs (`app/core/security.py`),
  argon2 password hashing, refresh-token revocation via a Redis-backed
  session store (`app/modules/auth/infrastructure/token_session_store.py`).
- Authorization: RBAC. A user may hold several roles
  (`app/modules/roles`, `user_roles` association table); the JWT carries
  one **active role** (`active_role` claim), switchable without a full
  re-login via `POST /auth/switch-role`, which reissues a token pair
  after verifying the requested role is one of the user's assigned
  roles. `require_role(...)` checks the active role;
  `require_permission(...)` checks all assigned roles' permissions.
- Sessions: access and refresh tokens carry a `sid` (session id) claim, kept across refresh
  rotation and role switching. Logout and refresh-token reuse revoke the *session* through a
  Redis denylist (so the access token dies too, and a reused token ends its whole family); it
  fails open if Redis is down. See [ADR 0010](0010-session-revocation.md).
- A login attempt by a correct-credentials-but-deactivated user is `403 account_disabled`
  (not `401`): the credentials were right, the account is what is refused.
- Tenant-management permissions (`tenants:*`) are platform-only:
  [ADR 0012](0012-tenant-provisioning.md).

## Consequences
- No external identity provider dependency for the base project — easy
  to run and test standalone.
- Migrating to an external OIDC provider (Keycloak/Auth0/etc.) later
  means replacing `app/modules/auth`'s token issuance with a token
  *verification* adapter against that provider; the RBAC model
  (`sys_roles`/`sys_user_roles`, `require_permission`) is designed to keep
  working either way since it doesn't assume this module issued the
  token, only that it can be decoded/verified.
- Permissions are a flat `list[str]` on `Role` (with `"*"` as wildcard)
  rather than a separate `permissions` table. This is intentionally
  simple for a base project; if a real product needs a full permission
  catalog with metadata (descriptions, grouping, UI hints), extract
  `permissions` into its own aggregate and record a new ADR rather than
  bolting metadata onto the string list.
