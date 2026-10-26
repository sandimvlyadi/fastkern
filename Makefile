.PHONY: help install dev lint format typecheck test test-cov arch-check precommit \
        migrate migration downgrade seed run worker docker-up docker-dev docker-down docker-seed \
        docker-seed-dev clean e2e e2e-ratelimit e2e-destructive docs docs-build

help:
	@echo "make install      - install dependencies via uv + install pre-commit hooks"
	@echo "make dev          - run API with reload"
	@echo "make lint         - ruff check"
	@echo "make format       - ruff format"
	@echo "make typecheck    - mypy strict"
	@echo "make test         - run pytest"
	@echo "make test-cov     - run pytest with coverage report"
	@echo "make arch-check   - enforce clean-architecture dependency rules"
	@echo "make precommit    - run all pre-commit hooks against the whole repo"
	@echo "make migration    - create new alembic revision (m='message')"
	@echo "make migrate      - apply migrations"
	@echo "make downgrade    - revert last migration"
	@echo "make seed         - seed baseline tenant/roles/users (idempotent)"
	@echo "make worker       - run celery worker"
	@echo "make docker-up    - start docker-compose stack"
	@echo "make docker-dev   - run the docker-compose stack with hot reload (foreground, like make dev)"
	@echo "make docker-down  - stop docker-compose stack"
	@echo "make docker-seed  - seed the docker-compose stack (needs it running)"
	@echo "make docker-seed-dev - seed a stack started with make docker-dev, keeping hot reload"
	@echo "make e2e          - end-to-end tests against the running, seeded stack"
	@echo "make e2e-ratelimit- rate-limit e2e (restarts the api with tiny limits, then restores)"
	@echo "make e2e-destructive - outage e2e (stops/starts redis and db)"
	@echo "make docs         - preview the documentation site with live reload (port 8001)"
	@echo "make docs-build   - build the documentation site into site/, failing on any warning"

install:
	uv sync --all-groups
	uv run pre-commit install

dev:
	uv run uvicorn app.main:app --reload --app-dir src --host 0.0.0.0 --port 8000

lint:
	uv run ruff check .

format:
	uv run ruff format .
	uv run ruff check --fix .

typecheck:
	uv run mypy src

# `env -u`: the suite defaults to SQLite; a DATABASE_URL exported for the dev server must not
# leak in (the schema fixture would refuse it). To run on Postgres, see docs/testing.md.
test:
	env -u DATABASE_URL uv run pytest

test-cov:
	env -u DATABASE_URL uv run pytest --cov-report=html

arch-check:
	uv run lint-imports

precommit:
	uv run pre-commit run --all-files

migration:
	uv run alembic revision --autogenerate -m "$(m)" --rev-id "$$(uv run python scripts/next_migration_id.py)"

migrate:
	uv run alembic upgrade head

downgrade:
	uv run alembic downgrade -1

seed:
	uv run python scripts/seed.py

worker:
	uv run celery -A app.core.celery_app worker --loglevel=info

docker-up:
	docker compose up -d --build

# Foreground, with compose `watch`: api reloads on code change, worker and beat restart on it.
# Layered on docker-compose.dev.yml; see that file for what reloads and what needs a rebuild.
DEV_COMPOSE = docker compose -f docker-compose.yml -f docker-compose.dev.yml
docker-dev:
	$(DEV_COMPOSE) up --build --watch

docker-down:
	docker compose down

# For a stack started with `make docker-up` (no hot reload). Do NOT use this against a stack
# started with `make docker-dev`: `seed` depends_on `api`, and this plain `docker compose`
# invocation only knows docker-compose.yml, not the dev overlay -- it silently recreates `api`
# without hot reload/the source bind-mount. Use `make docker-seed-dev` for a `docker-dev` stack.
docker-seed:
	docker compose --profile tools build seed
	docker compose --profile tools run --rm seed

# For a stack started with `make docker-dev`: same seed, but through $(DEV_COMPOSE) so `api`
# keeps its dev overlay (hot reload + source bind-mount) instead of getting silently recreated
# without it.
docker-seed-dev:
	$(DEV_COMPOSE) --profile tools build seed
	$(DEV_COMPOSE) --profile tools run --rm seed

# --- End-to-end tests (tests/e2e) -----------------------------------------
# They drive a RUNNING, SEEDED stack over HTTP: `make docker-up && make docker-seed` first.
# Each target recreates the api with the rate limits its tests need (the general suite makes far
# more calls than the defaults allow; the rate-limit tests need tiny limits) and waits for /readyz.
E2E_COMPOSE = docker compose -f docker-compose.yml -f docker-compose.e2e.yml
E2E_RATELIMIT_COMPOSE = docker compose -f docker-compose.yml -f docker-compose.e2e-ratelimit.yml
E2E_READY = for i in $$(seq 1 60); do curl -sf http://localhost:8000/readyz >/dev/null && exit 0; sleep 2; done; echo "stack not ready at http://localhost:8000/readyz" >&2; exit 1

e2e:
	$(E2E_COMPOSE) up -d --build api worker beat
	@$(E2E_READY)
	uv run pytest -m "e2e and not e2e_ratelimit and not e2e_destructive and not e2e_clamav" tests/e2e --no-cov

e2e-ratelimit:
	$(E2E_RATELIMIT_COMPOSE) up -d --build api worker
	@$(E2E_READY)
	uv run pytest -m e2e_ratelimit tests/e2e --no-cov; status=$$?; docker compose up -d api; exit $$status

# Stops and restarts redis/db containers: only against a stack you can afford to bounce.
e2e-destructive:
	$(E2E_COMPOSE) up -d --build api worker
	@$(E2E_READY)
	uv run pytest -m e2e_destructive tests/e2e --no-cov

# Needs a stack started with the clamav profile and MALWARE_SCAN_BACKEND=clamav (CI: docker-e2e-clamav).
# Not runnable on arm64 hosts: the clamav/clamav image publishes amd64 only.
e2e-clamav:
	uv run pytest -m e2e_clamav tests/e2e --no-cov

# --- Documentation site (docs/, mkdocs.yml) ---------------------------------
# Port 8001: `make dev` and the compose stack already use 8000.
docs:
	uv run zensical serve -a localhost:8001

docs-build:
	uv run zensical build --strict

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml site .cache
