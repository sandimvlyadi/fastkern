# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Versions 0.1.0 and 0.2.0 were developed before this repository was published, so their tags
are not part of its history.

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
