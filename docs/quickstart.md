# Try it from the image

Run the published image on your own machine to see what fastkern gives you before you build on it.
No checkout, no build and no Python: one compose file and Docker with Compose v2.

## Start the stack

```bash
curl -fsSLO https://raw.githubusercontent.com/sandimvlyadi/fastkern/main/docker-compose.quickstart.yml
docker compose -f docker-compose.quickstart.yml up -d
docker compose -f docker-compose.quickstart.yml --profile tools run --rm seed
```

The first command downloads the compose file into the current directory. `up` pulls
`ghcr.io/sandimvlyadi/fastkern:latest` with PostgreSQL, Redis and Mailpit, and runs the database
migrations when the API starts. The seed waits until the API is healthy, then creates a tenant,
four roles and one user per role.

After about a minute every service reports healthy:

```bash
docker compose -f docker-compose.quickstart.yml ps
```

| What | Where |
|---|---|
| API docs (Swagger UI) | <http://localhost:8000/docs> |
| Health / readiness | <http://localhost:8000/healthz> · <http://localhost:8000/readyz> |
| Metrics | <http://localhost:8000/metrics> |
| Mailpit (outgoing email) | <http://localhost:8025> |

The API is served under `/api/v1`. `/readyz` reports `beat` as `error` for the first minute, until
the scheduler's first heartbeat. That check is informational and never fails readiness.

## Log in

The seed creates these accounts in the tenant `00000000-0000-0000-0000-000000000000`, all with the
password `ChangePlease123!`:

| Email | Role |
|---|---|
| `developer@example.com` | developer (platform, every permission) |
| `superadmin@example.com` | superadmin (platform, every permission) |
| `admin@example.com` | admin (users, roles, files, audit log and the example modules of its own tenant) |
| `user@example.com` | user (no permissions; only its own profile) |

In Swagger UI, call `POST /api/v1/auth/login`:

```json
{
  "tenantId": "00000000-0000-0000-0000-000000000000",
  "email": "admin@example.com",
  "password": "ChangePlease123!"
}
```

Copy `accessToken` from the response into **Authorize** at the top of the page, and every other
endpoint is open to you with that account's permissions. `POST /api/v1/auth/register` creates a
new user in the same tenant; its welcome email shows up in Mailpit.

## Pick a version

The compose file runs `latest`, the newest release. To run a specific one, set `FASTKERN_IMAGE`:

```bash
FASTKERN_IMAGE=ghcr.io/sandimvlyadi/fastkern:0.3.0 \
  docker compose -f docker-compose.quickstart.yml up -d
```

The tags are listed on the
[package page](https://github.com/sandimvlyadi/fastkern/pkgs/container/fastkern).

## Clean up

```bash
docker compose -f docker-compose.quickstart.yml down -v
```

`-v` also removes the database and uploaded files. Leave it out to keep the data for the next `up`.

## What this is not

The stack runs with `ENVIRONMENT=local`, a public placeholder `SECRET_KEY` and public seed
passwords, and publishes its ports on `127.0.0.1` only. That suits a laptop, not a server: the app
refuses this configuration under `ENVIRONMENT=production`. To run fastkern for real, see
[Deploying to a VPS](./deploy-vps.md).

The image holds the platform modules (authentication, tenants, users, roles, files, audit log, the
partner API) and the reference modules meant to be copied (`examples`, `master_examples`,
`posts`). Your own business modules come from building on the code, which is the next step:
[Build your service](./new-service.md).
