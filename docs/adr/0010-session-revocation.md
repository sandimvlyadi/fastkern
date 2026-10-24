# 10. Session revocation and refresh-token reuse detection

## Status
Accepted. Extends [ADR 0004](0004-jwt-rbac-role-switching.md).

## Context
ADR 0004 revokes *refresh* tokens through a Redis store, but two gaps remained:

- **Logout did not end the session.** It deleted the refresh token, yet the access token
  issued alongside it stayed valid until it expired (`ACCESS_TOKEN_EXPIRE_MINUTES`, 15 by
  default). `GET /auth/me` with a logged-out access token still answered 200.
- **Refresh-token reuse only rejected the reused token.** Presenting an already-consumed
  refresh token A returned 401, but the token B that A had legitimately been rotated into
  stayed valid. Rotation with reuse detection (OAuth 2.0 Security BCP) requires ending the
  whole family: if A is replayed, either the client or an attacker holds a stale copy and we
  cannot tell which holds B.

## Decision
- **A session id (`sid`) claim** — a UUID minted at login — is carried by *both* the access
  and the refresh token. Refresh rotation and `POST /auth/switch-role` keep the caller's `sid`;
  every login is a new session (a user's other logins are unaffected).
- **Revocation is a Redis denylist keyed by session**: `revoked_sid:{sid}` (value `"1"`, with a
  TTL). Two things write it:
  - `POST /auth/logout`, after deleting the refresh token;
  - the refresh use case, when it sees a refresh token whose signature and expiry are valid but
    which is no longer in the store (already used, or already logged out) — the reuse signature.
- **Two things read it**: `get_current_user` (so every authenticated request rejects a revoked
  session's access token with `401 invalid_token`) and the refresh use case (`401 token_revoked`).
- **The denial lasts as long as the refresh token can** (`REFRESH_TOKEN_EXPIRE_MINUTES`), not
  as long as an access token. `switch-role` mints a new refresh token without revoking the
  previous one, so a denial that expired after 15 minutes would let that older refresh token
  revive the session. After the TTL every token of the session has expired anyway.
- **Only tokens that pass signature and expiry checks can revoke anything.** A foreign or
  expired token fails decoding before any of this, so nobody can revoke someone else's session
  by presenting garbage.
- **Tokens minted before this change carry no `sid`** and stay valid until they expire; the
  first refresh of one upgrades it to a session.
- **`activeRole` is validated on refresh** (D5): the claim outlives a revoked assignment, so
  refresh keeps it only if it is still assigned and otherwise falls back to the user's first
  assigned role (as login does); `/auth/me` reports it only while it is still assigned.
  Authorization was never affected (`require_permission` reads the database) — this removes a
  stale, misleading value.

### Redis unavailable: fail open
The denylist lookup happens on every authenticated request. If Redis is unreachable it returns
"not revoked", logs `session_denylist_unavailable`, and increments the Prometheus counter
`session_denylist_backend_errors_total`. This matches the rate limiter (ADR 0007): an outage
must not turn every authenticated request into an error. The cost is that during the outage a
logged-out access token stays usable until it expires (at most `ACCESS_TOKEN_EXPIRE_MINUTES`).
Alert on the counter. Operations that *write* Redis state (login, register, refresh, logout)
still fail with a clean `503` + `Retry-After`. The shared Redis pool has bounded socket
timeouts (`REDIS_SOCKET_TIMEOUT_SECONDS`, 2 s) so a black-holed Redis fails fast instead of
hanging requests.

## Consequences
- One extra Redis round trip per authenticated request.
- A client that fires two refreshes with the same token concurrently now ends its own session
  (one gets 200, the other triggers reuse detection). Clients must serialise refresh calls.
- Revoking *everything* a user holds is still not possible: `revoke_all_for_user` deletes
  refresh tokens but cannot enumerate access tokens. A per-user "tokens issued before" epoch
  would close that (e.g. for password change or admin-forced sign-out); not needed yet.
- `sid` is an opaque identifier for the server's bookkeeping; it is not an authorization input.
