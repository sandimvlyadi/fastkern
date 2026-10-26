# Build your service

Fastkern is a starting point: you copy the code, keep the platform it already has
(authentication, tenancy, RBAC, files, audit log, background jobs, observability) and add your own
business modules on top. This page takes you from a copy of the repository to a deployed release.

## 1. Create your repository

Either works; pick by whether you want fastkern's later changes.

| | **Use this template** | **Fork** |
|---|---|---|
| How | The **Use this template** button on the [repository page](https://github.com/sandimvlyadi/fastkern) | The **Fork** button |
| History | Starts fresh, with one commit | Keeps fastkern's history |
| Later fastkern changes | Copied over by hand, or cherry-picked after `git remote add upstream` | `git fetch upstream` and merge |
| Visibility | Public or private | Follows the original (public) |

Most services start from the template and treat fastkern's later releases as something to read
and copy selectively, not merge: once you have your own modules and migrations, an upstream merge
touches the same files you changed.

Then clone it:

```bash
git clone https://github.com/<you>/<your-service>.git
cd <your-service>
```

## 2. Make it yours

Rename the service and replace what identifies the original project. Search for `fastkern` and
`sandimvlyadi` to find every place.

| What | Where |
|---|---|
| Project name and description | `pyproject.toml` (`name`, `description`), then `uv lock` |
| Service name in logs, traces and `/healthz` | `app_name` and `otel_service_name` in `src/app/core/config.py`, `APP_NAME` and `OTEL_SERVICE_NAME` in `.env.example` and `.env.production.example` |
| Database name | `fastkern` in `docker-compose*.yml` and the `DATABASE_URL` in `.env.example` |
| Image name in docs and examples | `ghcr.io/sandimvlyadi/fastkern` in `.env.production.example`, `docker-compose.quickstart.yml` and `docs/`. The publish workflow already names the image after your repository |
| Owner and contacts | `LICENSE`, `README.md` (author), `SECURITY.md`, `CODE_OF_CONDUCT.md`, `.github/ISSUE_TEMPLATE/config.yml` |
| Changelog | Empty `CHANGELOG.md` down to its header: the history is fastkern's, not yours |
| Documentation site | `site_name`, `site_url` and `repo_url` in `mkdocs.yml`, and the repository name in the `deploy` job's `if:` in `.github/workflows/docs.yml`. Or delete both if you don't want a site |

The reference modules (`examples`, `master_examples`, `posts`) exist to be copied. Keep them until
your first module works, then remove them together with a migration that drops their tables.

## 3. Run it locally

```bash
cp .env.example .env
make install                      # dependencies and pre-commit hooks
docker compose up -d --build      # the whole stack, image built from your code
docker compose --profile tools run --rm seed
```

The other ways to run it (hot reload in Docker, the API on the host) are in
[Development](./development.md).

## 4. Add your modules

```bash
python scripts/new_module.py orders
```

The generator creates the four layers and registers the module with the architecture checks. The
[module guide](./module-guide.md) walks through entity, port, use case, model, repository and
router. Read [Architecture](./architecture.md) first for the rules that `make arch-check` and the
test suite enforce, and [Security model](./security.md) for tenancy, RBAC and rate limiting.

Before every pull request, run what CI runs:

```bash
make lint typecheck arch-check test precommit
```

`make test` needs Redis running; see [Testing](./testing.md) for the PostgreSQL and end-to-end
suites.

## 5. Publish an image

Pushing a `vX.Y.Z` tag runs `.github/workflows/publish-image.yml`, which scans and smoke-tests the
image, then publishes `ghcr.io/<you>/<your-service>` for amd64 and arm64. The tag must match the
version in `pyproject.toml`. The full release checklist is in
[ADR 0018](./adr/0018-release-process.md), and the image publishing rules in
[ADR 0022](./adr/0022-container-image-publishing.md).

The first publish creates the package as private. Make it public in the package settings on
GitHub, or keep it private and run `docker login ghcr.io` on the servers that pull it.

## 6. Deploy

[Running in production](./production.md) lists what the app refuses to start without and what
stays your responsibility. [Deploying to a VPS](./deploy-vps.md) runs the published image on one
server with Docker Compose and nginx, including rollouts, rollbacks and backups.
