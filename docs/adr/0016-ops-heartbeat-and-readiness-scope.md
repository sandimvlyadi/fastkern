# 16. Ops heartbeat and the scope of readiness

## Status
Accepted

## Context
Beat (scheduled jobs, `files.cleanup`) and ClamAV (optional malware scan, `MALWARE_SCAN_BACKEND=clamav`)
both run outside the request path, and neither was visible to operators:

- `/readyz` checked only the database, Redis and the Celery broker connection. A dead worker
  or beat left it green while jobs queued up or never ran.
- beat's only health signal was a Compose healthcheck that searches `/proc`. It works in Docker and
  nowhere else.
- A ClamAV outage made every upload `FAILED` (`SCAN_UNAVAILABLE`) with no metric and no alert.

Making these checks gate `/readyz` would be wrong. A dead scheduler or scanner would take every
healthy API replica out of rotation, although requests that do not touch uploads or jobs still work.

## Decision
- **Heartbeat task.** beat schedules `ops.heartbeat` every `OPS_HEARTBEAT_INTERVAL_SECONDS` (default
  60). A worker runs it and writes `ops:status:beat = ok` to Redis with a TTL of three intervals.
  Writing the key proves that beat fired, the broker delivered the task and a worker ran it.
- **ClamAV self-test, in the worker.** When `MALWARE_SCAN_BACKEND=clamav`, the same task scans a
  harmless sample with `zINSTREAM` and writes `ops:status:clamav = ok|error` with the same TTL. `ok`
  requires a clean verdict, so it proves clamd is up and its signature database loaded. A PING alone
  only shows that the socket answers, so it cannot show that scans work. When the backend is `none`, it
  deletes that key. The API never pings ClamAV on the request path.
- **Readiness.** `/readyz` reads the keys with `GET`. The `checks` object always has `beat`, and has
  `clamav` only when it is the scan backend.
- **Gating.** Only `database`, `redis` and `celery` (the broker) decide `status` and the HTTP code.
  `beat` and `clamav` are informational. A dead beat or scanner is reported in `checks` and surfaces
  through metrics and alerts, not through a 503.
- **Metrics.** `ops_check_up{check="beat"|"clamav"}` is computed at scrape time from the same keys.
  The clamav series is NaN when scanning is off, so deployments without ClamAV do not alert.
- **Why Redis.** Redis is already a required dependency and is the Celery broker. Keys with a TTL
  expire on their own, so a stopped beat or worker turns into `error` without any cleanup. The
  alternative, a process check through `/proc` or Kubernetes liveness, only works in some runtimes.
- **Celery check.** The `celery` check still tests broker reachability, which is what the API needs
  to dispatch jobs. Worker and beat liveness are reported by the heartbeat.

## Consequences
- Positive: a dead worker, beat or ClamAV is visible on `/readyz` and in `/metrics` without making
  the API unavailable. The heartbeat also covers non-Docker deployments.
- Negative: beat and clamav status can be up to one interval stale. With a 60 s interval the status
  can lag by a minute, and the key only expires after three intervals.
- Negative: `ops:status:*` keys share Redis with the rate limiter and session denylist. The keys are
  small and expire, so this is acceptable.
- Negative: the API and the worker must share `MALWARE_SCAN_BACKEND`. The worker writes the clamav key
  from its own setting, and the API reads only that key. A worker with `none` and an API with `clamav`
  leaves `clamav: error` in place indefinitely. Compose satisfies this through the shared `.env`.
- Negative: the first beat only runs one interval after beat starts, so a freshly started stack shows
  `beat: error` for up to 60 s.
- Not covered: the self-test proves that signatures load and scans work, not that they are current.
  A stale but loaded database still reports `ok`. Freshness depends on the ClamAV image's own updates.
  A real file that fails for another reason still shows up as `FAILED`.
- Follow-up: alert on `ops_check_up == 0` and on `rate(files FAILED with SCAN_UNAVAILABLE)`.
