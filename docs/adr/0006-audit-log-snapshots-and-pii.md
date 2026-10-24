# 6. Audit-log before/after snapshots and PII

## Status
Accepted

## Context
Every mutating endpoint records an audit event via
`app.core.di.record_audit_event(...)` with optional `before`/`after`
snapshots of the affected resource. Those snapshots make the trail useful
(who changed what, from what to what) but they copy user-identifying data
(`email`, `fullName`) into `sys_audit_logs`, a table that is append-only and is
NOT covered by the soft/hard-delete of the source row. A GDPR erasure of a
user (`repository.delete(id, hard=True)`) therefore does not remove that
user's data from the audit trail.

## Decision
- Snapshots are built by an explicit `audit_snapshot()` method on the
  entity (or a small DTO for relations), never by serializing the whole
  entity: secrets (`hashed_password`, tokens) can never reach the trail by
  adding a field to an entity. Keys are camelCase, matching the API.
- Audit rows keep the PII that a snapshot contains (email, full name).
  This is deliberate: the audit trail must stay able to answer "who was
  this user" after the account is gone, and audit rows are not user-owned
  data. Only the fields a diff or an investigation needs go into a
  snapshot; do not add fields "just in case".
- A GDPR/compliance erasure flow that hard-deletes a user MUST also redact
  the PII keys (`email`, `fullName`) in that user's audit rows
  (`resource_type = "user"`, `resource_id = <user id>`) in the same
  transaction, and note it in the commit. No such flow exists yet; when one
  is added it owns this redaction (it is not a job for the audit-log
  module's write path, which stays append-only).
- No retention/expiry policy is defined yet. Before this runs in
  production for a regulated tenant, define one and record it in a new ADR.

## Consequences
- Adding a snapshot field that is personal data is a policy change: name
  it in the PR and update the redaction list above.
- Because redaction is deferred to the future erasure flow, "hard delete a
  user" is not yet a complete erasure. That gap is known and accepted for
  the base project, not an oversight.
