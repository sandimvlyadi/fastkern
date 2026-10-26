# Fastkern

A production-ready starting point for FastAPI backend services: a modular monolith built on Clean
Architecture, with authentication, multi-tenancy, RBAC, background jobs, file uploads, a partner
API, observability and i18n already wired in and enforced by tooling.

Start new services from it instead of rebuilding the same infrastructure each time, and spend your
effort on business modules.

## Get started

<div class="grid cards" markdown>

-   **Try it from the image**

    ---

    Run the published image on your machine with one compose file: no checkout, no build. Log in
    with a seeded account and explore the API in Swagger UI.

    [Try it from the image](./quickstart.md)

-   **Build your service**

    ---

    Start your own repository from the template, add business modules, publish your own image and
    deploy it.

    [Build your service](./new-service.md)

</div>

## What you get

**Architecture.** Modular monolith with Clean Architecture; layer and module boundaries checked by
`import-linter`. A module generator and reference modules to copy. PostgreSQL, SQLAlchemy 2 (async)
and Alembic, with enforced table and column standards.

**Security and tenancy.** JWT access and refresh tokens with rotation, reuse detection and
server-side session revocation. Row-level multi-tenancy enforced in the repository layer. RBAC
with multiple roles per user and role switching. Rate limiting per IP, user and API key; startup
guards against unsafe production config.

**API conventions.** One response envelope, offset and cursor pagination, centralized error
mapping, translated error messages, datetimes always in UTC, OpenAPI docs that show the real
response and error shapes, and an audit log for every state-changing action.

**Files and partners.** Resumable chunked uploads with content-based type detection, optional
ClamAV scanning and image variants. Read-only partner endpoints behind scoped, rotatable API keys.

**Operations.** Celery workers and beat, structured JSON logs, OpenTelemetry tracing, Prometheus
metrics, health and readiness probes, a multi-architecture image on GHCR, and a reference
single-host deployment.

## Guides

| Guide | |
|---|---|
| [Architecture](./architecture.md) | Layers, modules, design principles, and the index of decisions (ADRs) |
| [Development](./development.md) | Ways to run the stack, make targets, email, pre-commit, adding a module |
| [Module guide](./module-guide.md) | Building a new module step by step |
| [Testing](./testing.md) | Test suites, PostgreSQL, end-to-end tests |
| [Security model](./security.md) | Authentication, tenancy, RBAC, partner API keys, rate limiting, files |
| [i18n and timezone](./i18n-and-timezone.md) | Translated messages and the UTC convention |
| [Observability](./observability.md) | Logs, traces, metrics, readiness, what to alert on |
| [Running in production](./production.md) | Startup guards, checklist, migrations |
| [Deploying to a VPS](./deploy-vps.md) | One server with Docker Compose and nginx: deploy, roll out, back up |
| [Rolling back a release](./rollback.md) | Undoing a bad release safely |

The source is on [GitHub](https://github.com/sandimvlyadi/fastkern), under the MIT license.
