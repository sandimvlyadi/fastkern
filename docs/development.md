# Development

## Ways to run the stack

### Everything in Docker

See the [Quickstart](../README.md#quickstart) in the README.

### Docker with hot reload

The whole stack in Docker, with code changes applied without a rebuild (the Docker counterpart of
`make dev`):

```bash
cp .env.example .env
make docker-dev
```

This runs in the foreground with `docker compose watch`. Migrations run when the API starts. Seed
with `make docker-seed-dev`, **not** `make docker-seed` or the plain
`docker compose --profile tools run --rm seed`:

```bash
make docker-seed-dev
```

The `seed` service depends on a healthy `api`. The plain `docker compose` command only knows
`docker-compose.yml`, not the `docker-compose.dev.yml` overlay, so it silently **recreates `api`
without hot reload** to match the config it knows. `make docker-seed-dev` runs the same seed
through both compose files, so `api` keeps its dev config.

What reloads:

| Change | Result |
|---|---|
| `src/**` (API code) | uvicorn reloads in place |
| `src/**` (tasks, worker, beat) | the `worker` and `beat` containers restart |
| `pyproject.toml`, `uv.lock`, `Dockerfile` | not rebuilt while `make docker-dev` runs. Stop it (Ctrl-C) and run `make docker-dev` again, which builds with `--build` |

Don't run `make docker-dev` together with `make docker-up` or `make dev`: they publish the same
ports (8000, 5432, 6379, 1025, 8025). It needs Docker Compose 2.32.0 or newer
(`docker compose version`) for the worker and beat restart rule.

If edits aren't picked up on macOS (Docker Desktop file sharing), set
`WATCHFILES_FORCE_POLLING=true` under `api` → `environment` in `docker-compose.dev.yml`. The watcher
then polls instead of relying on filesystem events, at some CPU cost.

### API on the host, dependencies in Docker

```bash
cp .env.example .env

make install
make docker-up
make migrate
make seed
make dev
```

The API listens on `http://localhost:8000`.

### Upgrading from a checkout that ran PostgreSQL 16

The stack runs PostgreSQL 18, which can't read a 16 data directory, and the `postgres_data` volume
now mounts at `/var/lib/postgresql` (PostgreSQL 18 keeps its data in a versioned subdirectory). If
`db` fails to start after pulling, either throw the local data away with `docker compose down -v`
and seed again, or dump it first with
`docker compose exec db pg_dumpall -U postgres > backup.sql` on the old version and restore it into
the new one.

## Make targets

| Command | Purpose |
|---|---|
| `make install` | Install dependencies with uv and the pre-commit hooks |
| `make dev` | Run the API with reload |
| `make lint` | Ruff lint |
| `make format` | Ruff format and fix |
| `make typecheck` | `mypy --strict` |
| `make arch-check` | Enforce the Clean Architecture boundaries |
| `make test` / `make test-cov` | Run pytest / with a coverage report |
| `make precommit` | Run every pre-commit hook against the whole repository |
| `make migration m="message"` | Create an Alembic revision (numbered `0001`, `0002`, …) |
| `make migrate` / `make downgrade` | Apply migrations / revert the last one |
| `make seed` | Seed the baseline tenant, roles and users (idempotent) |
| `make worker` | Run a Celery worker |
| `make docker-up` / `make docker-down` | Start / stop the compose stack |
| `make docker-dev` | Run the compose stack with hot reload |
| `make docker-seed` / `make docker-seed-dev` | Seed the compose stack / one started with `make docker-dev` |
| `make e2e`, `make e2e-ratelimit`, `make e2e-destructive` | End-to-end suites, see [Testing](./testing.md) |

## Email

Welcome emails are sent from the Celery worker through the configured `EMAIL_BACKEND`.

Outside Docker the default backend is `console`, which logs that a message would have been sent.
The compose stack uses `smtp` against Mailpit:

```text
SMTP: localhost:1025
UI:   http://localhost:8025
```

The API and worker containers reach it as `mailpit:1025` over the compose network. See
[ADR 0011](./adr/0011-notification-delivery.md) for the delivery design.

## Pre-commit and code quality

`make install` installs the pre-commit hooks. They run Ruff (lint and format), `mypy --strict`,
`import-linter`, gitleaks secret scanning, checks that code comments don't reference ADRs or
`CLAUDE.md`, and the usual whitespace, YAML, large-file, merge-conflict and private-key checks.

```bash
make precommit
```

Passing pre-commit keeps the `lint`, `typecheck` and `arch-check` CI jobs green, but it doesn't
replace the test suite. Comment style is described in
[CONTRIBUTING.md](../CONTRIBUTING.md#comments-and-docstrings).

## Adding a module

Use the generator rather than creating the folders by hand:

```bash
python scripts/new_module.py orders
```

It creates the four-layer structure under `src/app/modules/orders/` and registers the module with
`import-linter`. Then follow [the module guide](./module-guide.md).

## AI-assisted development

[`CLAUDE.md`](../CLAUDE.md) holds the rules an AI coding agent follows in this repository: the
same architecture, security, testing and quality standards expected from human contributors, in
a form an agent reads before changing anything.
