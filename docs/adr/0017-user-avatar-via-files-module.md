# 17. User avatar via the files module

## Status
Accepted

## Context
Users need a profile picture: upload, replace, and remove, usable both by the account holder
(self-service) and by `admin`/`superadmin`/`developer` managing another user. The files module
already handles storage, malware scanning, and image variant generation; this feature must not
duplicate any of that.

## Decision
- **Storage is a `FileAsset` row, not a parallel mechanism.** `sys_users.avatar_file_id` is a
  nullable FK to `trans_files.id` (`ondelete=SET NULL`). The `users` module's application layer
  directly reuses `files.application.use_cases.UploadSingleFileUseCase` and `DeleteFileUseCase`
  (not just their ports) rather than reimplementing upload race-safety/quota logic. This is a new
  cross-module pattern for this codebase, and it is sanctioned by the `import-linter` contracts in
  `pyproject.toml`: every "must not import other modules' infrastructure" contract's
  `source_modules` excludes `infrastructure`, so `users.application` importing
  `files.application`/`files.domain` is not a violation — only importing `files.infrastructure`
  directly would be (that still goes through `core/di.py`, the composition root, as usual).
- **Avatar visibility is always forced to `PUBLIC`.** Profile pictures are not sensitive, and this
  lets `avatarUrl` be served unauthenticated via the existing
  `GET /public/files/{file_id}/{variant}` endpoint, with no new serving code.
- **`avatarUrl` is eventually consistent by design.** It is derived from `avatarFileId` alone
  (`{api_v1_prefix}/public/files/{id}/original`), with no extra per-request `FileStatus` lookup —
  so it is populated in the response immediately after a `202 Accepted` upload, but can 404 for the
  few seconds between accepting the upload and the background pipeline (`files.process_upload`)
  finishing. Clients poll `GET /auth/me` / `GET /users/{id}`, the same pattern already used for
  `GET /files/{id}` upload-status polling.
- **Non-image uploads are rejected twice.** A cheap, spoofable `Content-Type` check at accept time
  (`image/jpeg`, `image/png`, `image/webp`, `image/gif` only) gives fast feedback for the common
  case. The authoritative check is a new Celery task, `users.finalize_avatar`, dispatched alongside
  `files.process_upload`: it retries (same "retry until the file reaches a terminal status" pattern
  as `files.process_upload`'s own `FileNotYetVisibleError` handling) until the linked `FileAsset` is
  terminal, then — if it is not `(AVAILABLE, kind=IMAGE)` — detaches it from the user (only if the
  user's `avatar_file_id` still points at that exact upload, so it never clobbers a newer avatar
  change already in flight) and soft-deletes the file if it had been published as a non-image
  `GENERIC`/`DOCUMENT` asset.
- **Replacing an avatar while the previous one is still mid-pipeline is rejected outright**
  (`AvatarChangeInProgressError`, 409: `UPLOADED`/`SCANNING`/`PROCESSING` on the existing
  `avatar_file_id`) rather than risking an orphaned file from a best-effort delete racing the
  pipeline. Removing an avatar, by contrast, always detaches the field immediately and only
  attempts to delete the file when it is already `AVAILABLE`; a stuck non-terminal file is instead
  swept later by the existing `files.cleanup` beat job once it exceeds the stale-file threshold.
- **No new RBAC permission.** Admin avatar management reuses the existing `users:write` permission
  already granted to the tenant `admin` role and to `superadmin`/`developer` via the `*` wildcard.
  Self-service requires no permission beyond being authenticated, mirroring
  `POST /users/me/password`.
- **`avatar_max_bytes` (default 5 MiB)** is a dedicated, smaller cap than the general upload limits
  (`UPLOAD_CHUNK_MAX_BYTES`, default 20 MiB), enforced before the upload use case is even called.

## Consequences
- Positive: zero duplicated storage/scanning/image-processing code; avatars get the same
  malware-scan and metadata-stripping guarantees as any other upload "for free".
- Positive: the admin/self-service split and the use-case composition pattern are confirmed safe by
  `make arch-check` (no forbidden cross-module infrastructure import was introduced).
- Negative: a millisecond-scale race — two avatar changes for the same user started before either's
  status read lands — can still leave one extra orphaned file. This is an accepted, documented gap
  (not solved with a lock; there is no precedent for one elsewhere in this codebase), bounded by the
  existing `files.cleanup` sweep.
- Negative: `avatarUrl`'s eventual consistency means a client that renders it immediately after a
  202 may see a transient 404 — acceptable because every avatar-consuming client already needs a
  "no avatar yet" placeholder state regardless.
