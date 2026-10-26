# Fastkern · Enterprise FastAPI Boilerplate

A production-ready starting point for FastAPI backend services: a modular monolith built on Clean
Architecture, with authentication, multi-tenancy, RBAC, background jobs, file uploads, a partner
API, observability and i18n already wired in and enforced by tooling.

Start new services from it instead of rebuilding the same infrastructure each time, and spend your
effort on business modules.

**Documentation:** <https://sandimvlyadi.github.io/fastkern/>

## Features

**Architecture**
- Modular monolith with Clean Architecture; layer and module boundaries checked by `import-linter`
- A module generator, and reference modules to copy (CRUD, master data, an external API)
- PostgreSQL, SQLAlchemy 2 (async) and Alembic, with enforced table and column standards

**Security and tenancy**
- JWT access and refresh tokens with rotation, reuse detection and server-side session revocation
- Row-level multi-tenancy enforced in the repository layer
- RBAC with multiple roles per user, role switching, and no privilege escalation
- Rate limiting per IP, user and API key; security headers; startup guards against unsafe production config

**API conventions**
- One response envelope, offset and cursor pagination, and centralized error mapping
- Translated error messages (`Accept-Language`); datetimes always in UTC
- OpenAPI docs that show the real response and error shapes
- An audit log for every state-changing action

**Files**
- Resumable chunked uploads with content-based type detection
- Optional ClamAV malware scanning, and image variants re-encoded without metadata

**Partner API**
- Read-only partner endpoints authenticated by scoped, rotatable API keys
- Per-request traffic log with its own retention

**Operations**
- Celery workers and beat for background and periodic jobs
- Structured JSON logs, OpenTelemetry tracing, Prometheus metrics, health and readiness probes
- Unit, integration, PostgreSQL and end-to-end test suites; pre-commit with secret scanning

## Getting started

Both paths need Docker with Compose v2.

### Try it from the image

No checkout and no build: the published image from GHCR with PostgreSQL, Redis and Mailpit.

```bash
curl -fsSLO https://raw.githubusercontent.com/sandimvlyadi/fastkern/main/docker-compose.quickstart.yml
docker compose -f docker-compose.quickstart.yml up -d
docker compose -f docker-compose.quickstart.yml --profile tools run --rm seed   # tenant, roles, users
```

After about a minute every service is healthy, and:

| | |
|---|---|
| API docs (Swagger UI) | http://localhost:8000/docs |
| Health / readiness | http://localhost:8000/healthz · http://localhost:8000/readyz |
| Metrics | http://localhost:8000/metrics |
| Mailpit (outgoing email) | http://localhost:8025 |

The API is served under `/api/v1`. The seeded accounts, picking a release and cleaning up are in
[Try it from the image](./docs/quickstart.md).

### Build your service

Create your own repository with **Use this template** (or fork), then run it from your checkout:

```bash
cp .env.example .env              # works as is locally; change SECRET_KEY before deploying
make install                      # dependencies and pre-commit hooks (needs uv)
docker compose up -d --build      # the whole stack, image built from your code
docker compose --profile tools run --rm seed
```

[Build your service](./docs/new-service.md) covers what to rename, adding modules, publishing
your own image and deploying it. [Development](./docs/development.md) has the other ways to run the
stack, such as hot reload.

## Project layout

```text
src/app/
├── core/          # config, security, db, cache, logging, errors, DI, middleware
├── shared/        # base entity, ORM base and mixins, base repository
├── modules/       # feature modules, each with domain/application/infrastructure/presentation
├── locales/       # translation catalogs
└── main.py        # application composition

migrations/        # Alembic migrations (0001, 0002, …)
tests/             # unit/, integration/, e2e/
scripts/           # seed, module generator, CI checks
docs/              # guides and Architecture Decision Records
```

## Common commands

| Command | Purpose |
|---|---|
| `make install` | Install dependencies and the pre-commit hooks |
| `make dev` | Run the API on the host with reload |
| `make test` | Run the test suite (needs Redis running) |
| `make lint` · `make typecheck` · `make arch-check` | Ruff, `mypy --strict`, architecture boundaries |
| `make precommit` | Every pre-commit hook against the whole repository |
| `make migration m="…"` · `make migrate` | Create / apply a migration |

All targets are listed in [Development](./docs/development.md#make-targets).

## Documentation

The guides below are also published as a site with search:
<https://sandimvlyadi.github.io/fastkern/>.

| Guide | |
|---|---|
| [Try it from the image](./docs/quickstart.md) | Run the published image locally without a checkout |
| [Build your service](./docs/new-service.md) | From the template to a deployed release of your own service |
| [Architecture](./docs/architecture.md) | Layers, modules, design principles, and the index of decisions (ADRs) |
| [Development](./docs/development.md) | Ways to run the stack, make targets, email, pre-commit, adding a module |
| [Module guide](./docs/module-guide.md) | Building a new module step by step |
| [Testing](./docs/testing.md) | Test suites, PostgreSQL, end-to-end tests |
| [Security model](./docs/security.md) | Authentication, tenancy, RBAC, partner API keys, rate limiting, files |
| [i18n and timezone](./docs/i18n-and-timezone.md) | Translated messages and the UTC convention |
| [Observability](./docs/observability.md) | Logs, traces, metrics, readiness, what to alert on |
| [Running in production](./docs/production.md) | Startup guards, checklist, migrations |
| [Rolling back a release](./docs/rollback.md) | Undoing a bad release safely |
| [Deploying to a VPS](./docs/deploy-vps.md) | One server with Docker Compose and nginx: deploy, roll out, back up |

[`CLAUDE.md`](./CLAUDE.md) holds the rules AI coding agents follow in this repository.

## Contributing

Contributions are welcome. Read [CONTRIBUTING.md](./CONTRIBUTING.md) for the workflow, coding and
comment standards, and pull request expectations. Everyone taking part follows the
[Code of Conduct](./CODE_OF_CONDUCT.md). To report a vulnerability, see [SECURITY.md](./SECURITY.md).

## License

[MIT](./LICENSE)

## Author

**Sandi Mulyadi** · [sandimvlyadi@gmail.com](mailto:sandimvlyadi@gmail.com)
