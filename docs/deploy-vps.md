# Deploying to a VPS

How to run fastkern on a single Linux server with Docker Compose and nginx, from the published
image. Everything lives in [`docker-compose.prod.yml`](../docker-compose.prod.yml) and
[`deploy/nginx/`](../deploy/nginx/). The general production rules (startup guards, what is your
responsibility) are in [Running in production](./production.md). Read that first.

| Service | Role | Reachable from |
|---|---|---|
| `nginx` | TLS, redirects HTTP to HTTPS, proxies to the api, blocks `/metrics` | the internet (80, 443) |
| `api` | uvicorn | nginx only |
| `worker`, `beat` | Celery; exactly one `beat` | other containers |
| `db`, `redis` | PostgreSQL 18, Redis 8 with a password and an append-only file | other containers |
| `migrate`, `seed` | one-shot deploy steps (`ops` profile) | run on demand |
| `clamav` | optional malware scanner (`clamav` profile) | other containers |

All app services run the one image named by `FASTKERN_IMAGE`, so the api, worker and beat are
always the same release.

The commands below assume the stack lives in `/opt/fastkern`, and use this alias:

```bash
cd /opt/fastkern
alias dc='docker compose -f docker-compose.prod.yml --env-file .env.production'
```

## 1. Prepare the server

- A 64-bit Linux server (x86_64 or arm64; the image is published for both) with at least 2 GB of
  RAM, or 4 GB with ClamAV.
- Docker Engine with the Compose plugin, from
  [Docker's own repository](https://docs.docker.com/engine/install/), not the distribution's
  older package (the commands use Compose v2's `docker compose`, not `docker-compose`).
- A firewall that allows only SSH, 80 and 443, for example with ufw:

  ```bash
  sudo ufw allow OpenSSH && sudo ufw allow 80/tcp && sudo ufw allow 443/tcp && sudo ufw enable
  ```

  Ports that Docker publishes bypass ufw. That is safe here because only nginx publishes any, but
  don't add a `ports:` entry to another service.
- SSH with keys only, and automatic security updates (`unattended-upgrades` on Debian/Ubuntu).

Put the deployment files of the release you deploy in `/opt/fastkern`: `docker-compose.prod.yml`,
`deploy/nginx/templates/` and `.env.production.example`. A shallow clone of the release tag gives
you exactly those, alongside the rest of the repository:

```bash
sudo git clone --depth 1 --branch v<VERSION> https://github.com/sandimvlyadi/fastkern.git /opt/fastkern
```

## 2. Configure

```bash
sudo cp .env.production.example .env.production
sudo chmod 600 .env.production
```

Fill in every `CHANGE-ME`:

| Setting | Value |
|---|---|
| `FASTKERN_IMAGE` | The release, best as the digest from the publish workflow's run summary: `ghcr.io/sandimvlyadi/fastkern@sha256:...`. Never `latest`. |
| `SERVER_NAME` | The public host name, e.g. `api.example.com` |
| `POSTGRES_PASSWORD`, `REDIS_PASSWORD` | `python3 -c "import secrets; print(secrets.token_hex(32))"`, one each. Hex, because both go into connection URLs. |
| `SECRET_KEY`, `METRICS_AUTH_TOKEN` | `python3 -c "import secrets; print(secrets.token_urlsafe(64))"`, one each |
| `CORS_ORIGINS` | The browser origins of your frontends |
| `SEED_DEFAULT_PASSWORD`, `SEED_DEVELOPER_EMAIL` | The initial accounts' password (12+ characters) and the developer account's email |

**Email.** Until a provider is set up, `EMAIL_BACKEND=console` with
`EMAIL_CONSOLE_IN_PRODUCTION=true` lets the stack start: nothing is sent, the worker only logs the
masked recipient and the subject, and the api logs `email_not_delivered` at every start. This needs
an image newer than 0.3.0, which refuses `console` in production outright. Switch to `smtp` or
`http` with the provider's credentials when mail has to reach people.

`ENVIRONMENT`, the database and Redis URLs and `FORWARDED_ALLOW_IPS` are set by the compose file;
don't set them in `.env.production`.

## 3. TLS certificate

Point the host name's DNS record at the server and check it resolves before asking for a
certificate (`dig +short api.example.com`). Then get the first certificate with certbot's webroot
method, with a throwaway nginx serving the challenge directory, because the real nginx can't start
until the certificate exists:

```bash
sudo apt install certbot
sudo mkdir -p /var/www/certbot
docker run -d --rm --name acme -p 80:80 -v /var/www/certbot:/usr/share/nginx/html:ro nginx:1.30-alpine
sudo certbot certonly --webroot -w /var/www/certbot -d api.example.com \
  --email you@example.com --agree-tos --no-eff-email
docker stop acme
```

Renewals then use the same webroot, served by the stack's nginx. certbot's package installs a timer
that renews automatically; add a hook so nginx loads the renewed certificate:

```bash
sudo tee /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh > /dev/null <<'EOF'
#!/bin/sh
cd /opt/fastkern && docker compose -f docker-compose.prod.yml --env-file .env.production exec -T nginx nginx -s reload
EOF
sudo chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
```

After the first deploy, `sudo certbot renew --dry-run` checks that renewal works.

Certificates are read from `/etc/letsencrypt` and challenges served from `/var/www/certbot`. Set
`TLS_CERT_DIR` / `ACME_WEBROOT` in `.env.production` to use other paths.

## 4. First deploy

```bash
dc pull
dc run --rm migrate          # alembic upgrade head
dc up -d --wait              # every service healthy, or this fails
dc run --rm seed             # once: baseline tenant, roles, permissions, initial accounts
curl -fsS https://api.example.com/readyz
```

Check:

- `dc ps` shows every service healthy.
- `dc logs api` shows only the startup warnings you accepted on purpose (`email_not_delivered`,
  `uploads_not_malware_scanned`).
- `curl -s -o /dev/null -w '%{http_code}' https://api.example.com/metrics` prints `404`.
- `dc logs nginx` shows your own IP for your requests. The api rate limits on the client IP that
  nginx passes on. If every request shows the same private address instead, something in front of
  nginx (a load balancer, Docker's userland proxy) hides the client address, and every client
  shares one rate-limit bucket.

## 5. Rolling out a release

1. Read the release's changelog and its new migrations. A release whose migrations are not
   additive needs the care described in [Keep releases rollback-safe](./rollback.md#keep-releases-rollback-safe).
2. Back up the database (section 7).
3. Set `FASTKERN_IMAGE` in `.env.production` to the new release's digest. Also update the
   deployment files when the release changed them: `git fetch --depth 1 origin tag v<VERSION>
   && git checkout v<VERSION>`.
4. Migrate, then restart on the new image:

   ```bash
   dc pull
   dc run --rm migrate
   dc up -d --wait
   ```

   The running api serves the new schema for the few seconds in between. That is safe because
   releases keep their migrations additive.
5. Verify as after the first deploy.

There is one api container, so `up` restarts it: requests fail with a 502 for a few seconds while it
comes back. A zero-downtime rollout needs a second api replica behind nginx and is out of scope here.

## 6. Rolling back

Set `FASTKERN_IMAGE` back to the previous release's digest and restart. Do **not** run `migrate`:
the older image doesn't know the newer schema's revision.

```bash
dc up -d --wait
```

The api no longer runs migrations at start, so this is all it takes when the release's migrations
were additive. If the old code can't run on the new schema, downgrade the schema first, **with the
new image still configured** (only it has the downgrade scripts), then switch the image:

```bash
dc run --rm migrate alembic downgrade <last revision of the previous release>
```

Read [Rolling back a release](./rollback.md) before doing that: a downgrade drops what the
migration added, data included.

## 7. Backups

**Database.** A custom-format dump, which `pg_restore` can restore whole or in part:

```bash
sudo mkdir -p /var/backups/fastkern
dc exec -T db pg_dump -U fastkern -d fastkern -Fc | sudo tee /var/backups/fastkern/db-$(date +%F).dump > /dev/null
```

**Uploaded files.** The `files_data` volume, archived from a container that mounts it:

```bash
dc run --rm --no-deps -T --user root -v /var/backups/fastkern:/backup --entrypoint tar api \
  -czf /backup/files-$(date +%F).tgz -C /app/data .
```

Redis is not backed up. It holds sessions and the task queue: its append-only file survives a
restart, and losing it logs every user out and drops queued tasks, but loses no records.

A daily job that keeps two weeks of backups, in `/etc/cron.d/fastkern-backup`:

```cron
30 2 * * * root cd /opt/fastkern && C='docker compose -f docker-compose.prod.yml --env-file .env.production' && $C exec -T db pg_dump -U fastkern -d fastkern -Fc > /var/backups/fastkern/db-$(date +\%F).dump && $C run --rm --no-deps -T --user root -v /var/backups/fastkern:/backup --entrypoint tar api -czf /backup/files-$(date +\%F).tgz -C /app/data . && find /var/backups/fastkern -type f -mtime +14 -delete
```

Copy the backups off the server as well (object storage, another host). A backup on the same
disk is lost with the server.

**Restore**, after stopping everything that writes:

```bash
dc stop api worker beat
dc exec -T db pg_restore -U fastkern -d fastkern --clean --if-exists --no-owner < /var/backups/fastkern/db-<DATE>.dump
dc run --rm --no-deps -T --user root -v /var/backups/fastkern:/backup --entrypoint tar api \
  -xzf /backup/files-<DATE>.tgz -C /app/data
dc up -d --wait
```

Test a restore now and then, into a scratch database so production is untouched:

```bash
dc exec -T db createdb -U fastkern restore_check
dc exec -T db pg_restore -U fastkern -d restore_check --no-owner < /var/backups/fastkern/db-<DATE>.dump
dc exec -T db psql -U fastkern -d restore_check -c 'select count(*) from sys_users'
dc exec -T db dropdb -U fastkern restore_check
```

## 8. Day to day

- Logs: `dc logs -f --tail 100 api worker`. They are JSON, one event per line.
- ClamAV: with `MALWARE_SCAN_BACKEND=clamav`, add `COMPOSE_PROFILES=clamav` to `.env.production`
  so every `dc up` starts it too.
- Disk: Docker keeps every image you pulled. `docker image prune -a` removes the unused ones;
  keep the previous release's image around until you are sure you won't roll back.
- Upgrading PostgreSQL to a new major version needs `pg_upgrade` or a dump and restore; it is not
  covered by a `dc pull`.
