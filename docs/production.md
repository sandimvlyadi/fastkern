# Running in production

Staging and production refuse to start on an unsafe configuration instead of silently running
with development defaults.

## Checklist

**Refused at startup** (with `ENVIRONMENT=production`; staging checks the secret key too):

- `DEBUG=true`
- A weak or placeholder `SECRET_KEY`. Generate one with
  `python -c "import secrets; print(secrets.token_urlsafe(64))"`
- A `*` in `CORS_ORIGINS`
- `EMAIL_BACKEND=console`, or `smtp`/`http` without credentials

**Refused by the seed script:** a missing or public-default `SEED_DEFAULT_PASSWORD`, a password
shorter than 12 characters, or a missing `SEED_DEVELOPER_EMAIL`.

**Logged at startup, accepted if deliberate:**

- `/metrics` without `METRICS_AUTH_TOKEN` (`metrics_endpoint_unauthenticated`): fine only if the
  reverse proxy keeps `/metrics` off the public listener
- Uploads without malware scanning (`uploads_not_malware_scanned`): set
  `MALWARE_SCAN_BACKEND=clamav` and run the `clamav` compose profile, or accept unscanned uploads

**Not checked, your responsibility:**

- `FORWARDED_ALLOW_IPS` set to the reverse proxy's address (see [Security](./security.md#behind-a-reverse-proxy))
- `RATE_LIMIT_*` values suited to your traffic
- Migrations run as a deployment step (below)
- Exactly one `beat` process per deployment: two would enqueue every periodic job twice
- Redis available: login, refresh and logout need it
- The API, worker and beat share one file storage volume

API documentation (`/docs`, `/redoc`, `/openapi.json`) is off in production by default. Set
`DOCS_ENABLED=true` to keep it on an internal deployment.

## Container images

Every release tag `vX.Y.Z` is published to `ghcr.io/sandimvlyadi/fastkern` for `linux/amd64`
and `linux/arm64`, tagged `X.Y.Z`, `X.Y` and `latest`. The image is public, so pulling it needs
no login:

```bash
docker pull ghcr.io/sandimvlyadi/fastkern:0.3.0
```

Pin a deployment to the version tag, or to the digest the publish workflow prints in its run
summary (`ghcr.io/sandimvlyadi/fastkern@sha256:...`). Never deploy `latest`: it moves with every
release, so a restart can silently change the running version, and a rollback has nothing to
point back to.

The same image runs every process type. Only the command differs:

| Process | Command |
|---|---|
| API | `uvicorn app.main:app --host 0.0.0.0 --port 8000` (the image's default) |
| Worker | `celery -A app.core.celery_app worker --loglevel=info` |
| Beat (exactly one) | `celery -A app.core.celery_app beat --loglevel=info --schedule=/tmp/celerybeat-schedule` |
| Migrations | `alembic upgrade head` |
| Seed (once) | `python scripts/seed.py` |

The image's `HEALTHCHECK` probes the API's `/healthz`, so the worker and beat need their own
health checks. `docker-compose.yml` has the ones to copy.

## Database migrations

Run migrations as an explicit deployment step, once, before the new version starts serving:

```bash
alembic upgrade head
```

The compose stack runs migrations when the API container starts. That is a local-development
convenience, not a production deployment model.

## Rolling back

To undo a bad release, and to keep releases safe to undo, see [Rolling back a release](./rollback.md).
