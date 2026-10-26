# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Versions 0.1.0 and 0.2.0 were developed before this repository was published, so their tags
are not part of its history.

## 0.3.0 - 2026-10-26

First release from this repository, which continues the project under the
name `fastkern`.

### Added

- **Partner API keys** (`api_clients` module): tenant-scoped keys sent in
  `X-API-Key`, with scope-based authorization independent of RBAC, expiry,
  rotation with an overlap window, and revocation. Partner calls get their
  own `PARTNER` rate-limit tier and a request traffic log with a retention
  purge job. Audit entries record whether a user or a partner acted
  (`actor_type`).
- **Partner read endpoints** under `/partners`: posts, users, roles (and a
  user's role assignments), master examples, examples, and files (metadata
  and content), each guarded by its own `<resource>:read` scope.
- **Permission and scope catalogs**: `GET /roles/permissions` and
  `GET /api-clients/scopes` list what a client can pick from, with a
  per-caller `grantable` flag.
- **Posts module**: an example of integrating an external HTTP API
  (JSONPlaceholder) through a domain port and an httpx + tenacity adapter.
- A startup warning (`metrics_endpoint_unauthenticated`) when production
  serves `/metrics` without `METRICS_AUTH_TOKEN`.
- Documentation in `docs/`: architecture, development, testing, security,
  i18n and timezone, observability, production, and a rollback runbook.
- CI: end-to-end jobs that boot the real Docker stack (general, rate-limit,
  outage, and real-ClamAV suites), a Trivy scan of the image for fixable
  HIGH/CRITICAL CVEs, Dependabot, and pre-commit hooks rejecting ADR and
  internal-plan references in code comments.

### Changed

- Python 3.14, with the dependency tree upgraded (FastAPI 0.161,
  SQLAlchemy 2.1, Pydantic 2.14, asyncpg 0.32, uvicorn 0.54).
- The compose stack runs PostgreSQL 18 and Redis 8, with every service
  image pinned. An existing PostgreSQL 16 dev volume must be recreated or
  upgraded with `pg_upgrade`.
- Creating a role, or adding a permission to one, rejects permissions
  outside the catalog (`422 unknown_permission`). `resource:*` is no longer
  accepted, since it never granted anything.
- Creating or rotating an API key requires the caller to hold an RBAC
  permission matching each requested scope (`403 scope_escalation`), and
  unknown scopes are rejected (`422 unknown_scope`). An expired key cannot
  be rotated (`409 api_key_already_expired`).
- The request's database transaction now commits before the response is
  sent, so a client never acts on a success whose commit later failed.
- The Docker image pins its Python base image by digest, copies a pinned
  `uv` instead of piping an install script, and drops `pip` from the
  runtime stage.
- The README is now a short overview; the details moved into `docs/`.

### Fixed

- A Redis broker outage returns `503` with `Retry-After` instead of a `500`
  or a stalled event loop: tasks are published from a worker thread
  (`dispatch_task`) without subscribing to a result.
- Patched vulnerable runtime dependencies (Pillow 13, certifi,
  OpenTelemetry), and `pip-audit` now fails the build on any finding.
- The posts adapter maps unexpected upstream responses to
  `PostsServiceError` (`502`) instead of a `500`, and no longer retries a
  `POST` that may already have been sent.
- The request-log and audit-log tasks actually retry transient database
  errors.
- Verifying an API key no longer takes a row lock, so concurrent calls with
  one key don't queue behind each other.

### Upgrade notes

- Migrations `0006`–`0008` add `sys_api_keys`, `sys_api_request_logs`, and
  `sys_audit_logs.actor_type`. Rolling back to 0.2.0 drops them; see
  `docs/rollback.md`.

## 0.2.0 - 2026-10-09

### Added

- **User roles**: self-service role listing (`GET /auth/me/roles`), role
  assignment/unassignment, and a persisted default role per user.
- **Example/master-example template modules**: `examples` and
  `master_examples`, full reference implementations (entity → port → use
  case → model → repository → router) new modules can be copied from.
- **File upload & image pipeline**: single and chunked (resumable) upload,
  magic-byte content detection, malware scanning (ClamAV, optional),
  Pillow-based image processing (orientation, size cap, metadata
  stripping, variants), and public/private file serving.
- **User avatars**: self-service and admin avatar upload/replace/remove via
  the files module, surfaced on `GET /auth/me` and `GET/PATCH /users/{id}`.
- **Password management**: self-service and admin password change
  endpoints.
- **Ops heartbeat**: a periodic beat → broker → worker heartbeat that feeds
  `/readyz`'s `beat` check and, when `MALWARE_SCAN_BACKEND=clamav`, a
  ClamAV self-test status.
- **Local Docker dev workflow**: `docker-compose.dev.yml` hot-reload
  overlay (`make docker-dev`) and a matching `make docker-seed-dev` that
  seeds without disturbing the dev overlay.
- **Sequential migration ids**: `make migration` now assigns sequential
  4-digit Alembic revision ids instead of random hex, enforced in CI.

### Changed

- Squashed the three pre-release migrations into a single baseline
  migration now that sequential ids are enforced.
- CI's ClamAV end-to-end job now runs on `amd64` runners only (the
  `clamav/clamav` image has no `arm64` build).

### Fixed

- `make docker-seed-dev` no longer silently recreates the `api` container
  without its hot-reload overlay when seeding a `docker-dev` stack.
- ClamAV readiness now uses a real scan self-test instead of a bare
  `PING`, so a clamd that accepts connections but can't actually scan is
  correctly reported as not ready.

### Internal

- Removed ADR cross-references and trimmed over-long explanatory comments
  throughout the codebase in favor of short, self-contained WHY comments,
  per `CLAUDE.md`'s comment conventions.

## 0.1.0 - 2026-09-28

Initial release. `fastkern` provides a production-oriented FastAPI
foundation — a modular monolith built on Clean Architecture — intended as a
starting point for new backend services rather than a demo application.

### Added

- **Architecture**: modular monolith with Clean Architecture layering
  (`domain` → `application` → `infrastructure` → `presentation`) per module,
  enforced in CI via `import-linter` (`make arch-check`).
- **Authentication**: JWT access/refresh tokens with session revocation
  support.
- **Authorization**: RBAC with multi-role switching, tenant-aware
  authorization, `require_permission`/`require_role` dependencies.
- **Multi-tenancy**: row-level tenant scoping enforced through
  `BaseRepository` and shared infrastructure.
- **Audit logging**: centralized audit event recording for mutating,
  tenant/user-visible actions.
- **Notifications**: notification delivery module with Celery-based
  dispatch and locale-aware content.
- **Internationalization**: `Accept-Language`-driven translation of all
  user-facing error messages and notification content, with locale
  catalogs (`en`, `id`) and CI-enforced key coverage.
- **Timezone handling**: UTC storage everywhere, with
  `X-Timezone`-aware conversion only at human-rendering edges (PDF/email/CSV).
- **API conventions**: standardized success/error response envelopes,
  offset and cursor pagination, centralized exception handling mapped to
  HTTP status codes.
- **Rate limiting**: configurable per-IP and per-tenant/user rate limits
  (`RateLimitTier.IP/DEFAULT/WRITE/AUTH`) with CI-enforced coverage across
  endpoints.
- **Observability**: structured JSON logging, OpenTelemetry tracing,
  Prometheus metrics, `/healthz` and `/readyz` with DB/Redis/Celery checks.
- **Caching**: Redis-backed caching.
- **Background jobs**: Celery task infrastructure, including tasks that
  safely manage their own DB engine/event loop.
- **Database**: PostgreSQL + SQLAlchemy + Alembic, with enforced table/column
  naming standards, `StandardModel`/`GlobalModel` base models, soft delete
  with actor tracking, and `updated_at` triggers checked in CI.
- **Tenant provisioning**: dedicated flow and validation for onboarding new
  tenants and users, including foreign-key-safe tenant validation during
  registration.
- **OpenAPI documentation**: every endpoint documents its real response
  shape via `SuccessEnvelope`, documents realistic error responses via
  `error_responses(...)`, and includes request/response examples.
- **Security**: pre-commit hooks (formatting, linting, type checking,
  `gitleaks` secret scanning) enforced both locally and in CI, a documented
  security policy, and a dedicated security issue template.
- **Developer experience**: `scripts/new_module.py` module generator,
  `docs/module-guide.md` walkthrough, Docker Compose development
  environment, and `Makefile` targets for lint/typecheck/arch-check/test.
- **Testing**: unit, integration, and end-to-end test suites, run against
  both SQLite and PostgreSQL in CI, with `create_db_tenant`/`create_db_user`
  fixtures for realistic Postgres-backed integration tests.
- **AI-agent development rules**: `CLAUDE.md` codifying the architecture,
  conventions, and CI-enforced rules for any AI coding agent working in
  this repository.
- **Architecture Decision Records**: 12 ADRs documenting the modular
  monolith/Clean Architecture choice, multi-tenancy strategy, JWT/RBAC
  design, i18n/timezone conventions, audit log/PII handling, rate limiting,
  observability backend, database standards, session revocation,
  notification delivery, and tenant provisioning (`docs/adr/`).
