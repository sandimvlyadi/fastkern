# 23. Reference deployment: one host with Docker Compose and nginx

## Status

Accepted

## Context

ADR 0018 deliberately picked no deployment platform, and ADR 0022 made each release a published
image. The first real deployment target is a single x86 VPS. The development compose file is not
fit to run there: it builds the image, runs migrations every time the api starts (which makes a
rollback to the previous image fail, see [Rolling back a release](../rollback.md)), publishes
PostgreSQL and a password-less Redis on every interface, and runs Mailpit.

The first deployment also has no email provider yet, and the only mail the application sends is
the welcome email. Production refused `EMAIL_BACKEND=console` outright (ADR 0011), so it could not
start without one.

## Decision

- **A standalone `docker-compose.prod.yml`**, not an override of the development file, so nothing
  from development leaks into production by merge. It runs the published image named by one
  `FASTKERN_IMAGE` variable for the api, worker, beat and the one-shot steps, so they can't drift
  apart. Restart policies on every long-running service. The project is named `fastkern-prod`, so
  it can't share volumes with a development stack on the same machine.
- **Migrations are an explicit step** (`run --rm migrate`, in an `ops` profile with `seed`), never
  part of the api's start. Rolling back is then only a change of `FASTKERN_IMAGE`.
- **nginx is the only service with published ports.** It terminates TLS with certbot certificates
  from the host (webroot renewals, a deploy hook reloads nginx), redirects HTTP to HTTPS, drops
  requests for unknown host names, allows request bodies of 25 MB (one 20 MiB upload chunk plus
  headers) and answers `/metrics` with 404. It resolves the api through Docker's DNS at run time,
  since a rollout gives the api container a new address.
- **Client IPs**: nginx overwrites `X-Forwarded-For` with the connecting address, and sits at a
  fixed address on its own network with the api; uvicorn trusts forwarded headers from that address
  only (`FORWARDED_ALLOW_IPS`), so rate limiting sees real clients and a client can't pick its IP.
- **Redis** requires a password and keeps an append-only file, because sessions live there.
- **Log-only email in production is an explicit opt-in**: `EMAIL_CONSOLE_IN_PRODUCTION=true` lets
  production start with `EMAIL_BACKEND=console`, and the api logs `email_not_delivered` at every
  start while it is on. Without the flag production still refuses `console`. The console backend
  logs only the masked recipient and the subject, so nothing sensitive reaches the logs.
- **The development compose file binds its published ports to 127.0.0.1**, and its app containers
  reach Mailpit by service name instead of through the host.
- CI validates the production compose file and runs `nginx -t` on the rendered nginx template.

The steps (server setup, certificate, first deploy, rollout, rollback, backup and restore) are in
[Deploying to a VPS](../deploy-vps.md).

## Alternatives

- **nginx on the host instead of in the stack.** One less container, but its config then lives
  outside the repository and can't be validated by CI, and it needs the api published on a host
  port.
- **OKD / Kubernetes.** The same image runs there with these differences: the random UID in group 0
  needs `/app/data` to be group-writable, the files volume needs a ReadWriteMany claim, and
  migrations run as a Job. Not done until a deployment needs it.
- **Mailpit, or another catch-all SMTP server, inside the production stack** instead of the opt-in.
  It would satisfy the startup check with fake credentials, hiding the same fact the opt-in states
  openly: mail doesn't reach anyone.

## Consequences

- Positive: a deployment is a pinned image plus three files from the repository, and a rollback is
  one variable.
- Positive: nothing but nginx is reachable from outside the host.
- Negative: one api container, so a rollout or rollback answers 502 for a few seconds. Zero-downtime
  rollouts need a second replica, which this layout doesn't have.
- Negative: everything runs on one machine. Backups have to be copied elsewhere to survive it.
- Negative: an operator can leave log-only email on longer than meant; the startup warning is the
  only reminder.
