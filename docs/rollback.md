# Rolling back a release

How to undo a bad deploy. This repo doesn't pick a deployment platform (ADR 0018 leaves that to
whoever deploys it), so the steps are written in terms of "the previous release's image" and use
`docker compose` for the examples. Translate them to your platform's equivalent.

## 1. Decide: roll back, or fix forward?

Roll back when the new release is broken and the cause isn't a one-line fix. Fix forward (ship a
patch release) when:

- the release ran a migration whose `downgrade()` would destroy data people now depend on (see
  step 3), or
- the bug is small and well understood, and a patch is faster than a rollback.

When in doubt, roll back the **application** (step 2) and leave the schema where it is. That is
always safe if the release followed the rule in "Keep releases rollback-safe" below.

## 2. Roll back the application

Redeploy the image built from the previous release tag (`vX.Y.Z`) for **all four process types
together**: `api`, `worker`, `beat` and the one-shot `seed` image if you use it. The worker and beat
must match the API: a newer API can enqueue task names an older worker doesn't know, and those
tasks fail.

Don't run `alembic upgrade head` as part of this. The production deploy runs migrations as a
separate step ([Running in production](./production.md#database-migrations)). An older image doesn't know the database's newer
revision, so `alembic upgrade head` there fails with `Can't locate revision`. The compose `api`
service runs exactly that on every start, so with compose, start the old API with uvicorn only:

```bash
git checkout v<PREVIOUS>
docker compose build api worker beat
docker compose up -d --no-deps worker beat
docker compose stop api
docker compose run -d --service-ports --name api-rollback api \
  uvicorn app.main:app --host 0.0.0.0 --port 8000
```

(If you also rolled the schema back in step 3, a plain `docker compose up -d api` works again.)

## 3. Roll back the schema (only if you must)

Only needed when the old code can't run against the new schema, for example because the new
release renamed or dropped a column the old code reads.

1. **Back up the database first.** A downgrade is not an undo: it runs the migration's
   `downgrade()`, which usually drops what `upgrade()` added, along with the data in it.
2. List the migrations the release added:

   ```bash
   git diff --stat v<PREVIOUS>..v<BAD> -- migrations/versions
   ```

3. Read every one of those `downgrade()` functions and note what it drops. For example, rolling
   v0.3.0 back to v0.2.0 (whose last migration is `0005`) would drop
   `sys_api_keys` and `sys_api_request_logs` (every issued partner API key and its traffic log)
   and `sys_audit_logs.actor_type` (who made each audit entry: user or partner).
4. Downgrade to the previous release's last revision **before** redeploying the old code, using
   the **new** image (it's the one that has the downgrade scripts):

   ```bash
   docker compose run --rm api alembic downgrade <last revision of v<PREVIOUS>>   # e.g. 0005
   ```

5. Then do step 2.

CI's `test-postgres` job runs `alembic downgrade base` on every PR, so the downgrade scripts are
known to run. That doesn't make them lossless.

## 4. Verify

- `GET /readyz` returns 200, and every service is healthy: `docker compose ps`.
- `alembic current` shows the revision you expect.
- Log in, and exercise whatever the bad release broke.
- Watch `rate_limit_backend_degraded`, the 5xx rate and the Celery worker logs for a few minutes.

## Keep releases rollback-safe

A release can always be rolled back by redeploying the previous image (step 2 alone) if its
migrations are **additive**: new tables, new nullable columns, new indexes. The old code ignores
them. Spread a destructive change (drop or rename a column, add NOT NULL to an existing column)
across two releases: first ship code that no longer needs the old shape, then drop it in a later
release. That way the release before it is always a safe rollback target.
