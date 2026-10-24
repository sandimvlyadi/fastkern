# Testing

## Standard suite

```bash
make test
```

The default suite runs against SQLite for speed, with coverage enforced at 95%.

**Start Redis first**: `docker compose up -d db redis mailpit` (or just `redis`). `make test`
doesn't need PostgreSQL or Mailpit, but Redis backs the token session store and is the Celery
broker. Without it, any test that logs in, refreshes or switches role fails with a raw connection
error rather than a normal assertion failure. Run `docker compose ps` and confirm the services are
`healthy` before treating a failure as a real bug.

## PostgreSQL

SQLite enforces neither foreign keys nor column lengths the way PostgreSQL does, so CI also runs
the whole suite and the migrations against PostgreSQL. To do the same locally:

```bash
docker run -d --rm --name pgtest -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=app_test \
  -p 55432:5432 postgres:18-alpine

DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:55432/app_test uv run pytest

docker stop pgtest
```

A test that inserts a tenant-owned row needs a real tenant (and a real user for role
assignments): use the `create_db_tenant` / `create_db_user` fixtures from `tests/conftest.py`.

## End-to-end

The tests in `tests/e2e` drive a **running, seeded stack over HTTP**. They cover registration and
login, RBAC, tenant isolation, races against real PostgreSQL, mail delivery through Mailpit,
production configuration refusal, dependency outages, rate limiting, file uploads and partner API
keys.

```bash
make docker-up
make docker-seed

make e2e
make e2e-ratelimit
make e2e-destructive
```

`make e2e-destructive` stops and restarts Redis and PostgreSQL. Run it only against a stack that
can safely be restarted.

## What a change needs

- New domain or application logic: a unit test in `tests/unit/` (no database, no HTTP client).
- A new endpoint: an integration test in `tests/integration/` using the `client` and
  `session_factory` fixtures.
- Before a PR: `make lint`, `make typecheck`, `make arch-check`, `make test` and `make precommit`.
