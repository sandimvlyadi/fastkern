# 7. Rate limiting

## Status
Accepted

## Context
`app/core/rate_limit.py` originally built a `slowapi` limiter that nothing
used: no route was decorated and no middleware was installed, so the API
was unlimited. Wiring it up as-is had three problems:

- slowapi's storage is synchronous, so every limit check would do blocking
  Redis I/O on the event loop.
- Its 429 handler answers `{"error": "..."}`, bypassing the response
  envelope and the i18n catalog (CLAUDE.md section 3).
- A middleware runs before authentication, so the tenant/user are unknown
  and the bucket could only be keyed by IP; and `/metrics` (a route owned
  by another library) cannot be excluded from a global middleware cleanly.

## Decision
- Rate limiting is a **FastAPI dependency**, `Depends(rate_limit(tier))`
  from `app.core.rate_limit`, built on the async API of `limits`
  (`limits.aio`, `implementation="redispy"` because `coredis` is not a
  dependency). slowapi is removed; `limits` is now a direct dependency.
- It raises `RateLimitedError`, so the 429 goes through the central handler
  in `app/core/errors.py` (envelope + translated message). The handler
  adds `Retry-After` and `X-RateLimit-Limit/Remaining/Reset`; successful
  responses carry the `X-RateLimit-*` headers too.
- Four tiers, all configurable (`RATE_LIMIT_IP` 300/minute,
  `RATE_LIMIT_DEFAULT` 100/minute, `RATE_LIMIT_AUTH` 10/minute,
  `RATE_LIMIT_WRITE` 30/minute): `IP` on every API router, `DEFAULT` on
  every API router too, `AUTH` on register/login/refresh, `WRITE` on top of
  `DEFAULT` for POST/PATCH/DELETE. Tiers have separate buckets.
- Bucket key: `ip : client-ip` for the `IP` tier; `tier : tenant : user-id`
  for authenticated callers and `tier : no-tenant : client-ip` before login
  for the others.
- **Order matters** (enforced by `tests/unit/test_rate_limit_coverage.py`):
  `IP` first, so requests that auth rejects (garbage/expired tokens, a
  deactivated user's still-valid token, which costs a DB query) are still
  counted; then `get_current_user`, so the other tiers can key on
  tenant + user; then `DEFAULT`, then `require_permission`, so a 403 also
  consumes quota (each one costs two DB queries).
- Fixed-window strategy: one Redis counter per bucket, the cheapest option.
  A caller can burst up to 2x the limit across a window boundary; switch to
  the moving-window strategy if that ever matters.
- **Fail-open with a circuit breaker**: if Redis is unreachable or slow
  (250 ms socket timeout) the request is served without limiting. Fail-open
  alone is not cheap: measured against a black-holed Redis, every request
  waited out one timeout per tier (~0.75 s each). So after a failure Redis is
  skipped entirely for 5 s (one `rate_limit_backend_unavailable` warning per
  outage, not per request), then a single probe request tests it while the
  others keep skipping; when it succeeds `rate_limit_backend_recovered` is
  logged and limiting resumes. Cost of an outage: one timeout per 5 s, and
  the requests already in flight when it starts.
- Configured limits must allow at least 1 request per window (`0/minute`
  parses but would reject everything, so `Settings` refuses it at startup).
- Browser clients on another origin can only read `Retry-After`,
  `X-RateLimit-*` and `X-Request-ID` because CORS `expose_headers` lists
  them (`main.py`); they are not CORS-safelisted.
- **Never limited**: `/healthz`, `/readyz`, `/metrics`, `/docs`, `/redoc`,
  `/openapi.json`. They are not on an API router, so no dependency is
  attached. Orchestrator probes and Prometheus scrapes must not get a 429.
- `RATE_LIMIT_ENABLED=false` turns the whole mechanism off (load tests,
  local experiments). pytest sets it, because every test client shares one
  IP; `tests/integration/test_rate_limit.py` re-enables it with an
  in-memory storage through `dependency_overrides[get_rate_limiter]`.
- `tests/unit/test_rate_limit_coverage.py` fails if an API route has no
  `rate_limit` dependency, if a mutating route only has the default tier,
  if a limited route doesn't document its 429, or if an operational
  endpoint got limited.

## Consequences
- A new endpoint that forgets `Depends(rate_limit(...))` fails the test
  suite instead of shipping unlimited. The module generator emits a router
  skeleton that already includes it.
- Behind a reverse proxy/load balancer `request.client.host` is the
  proxy's address, so every unauthenticated caller would share one bucket.
  Run uvicorn with `--proxy-headers` and set `FORWARDED_ALLOW_IPS` to the
  proxy's address(es) — never `*` on an internet-facing port, or
  `X-Forwarded-For` can be spoofed to dodge the limit.
- The `IP` tier is deliberately loose (300/minute): an office or mobile
  carrier behind one NAT shares it. It bounds abuse from a single address,
  it is not a per-user quota. Tighten it only if the traffic profile allows.
- Redis is on the request path: `DEFAULT`/`WRITE`/`AUTH` cost two round
  trips each (the hit, then the window stats for the `X-RateLimit-*`
  headers, ~1 ms locally per check); `IP` costs one when it allows the
  request (it sends no headers). An authenticated write endpoint passes
  through three tiers, so about five round trips. `hit` and the stats are
  separate calls, so if the window rolls over between them a denial's
  `Retry-After` is set to 1 s instead of the new window's full length.
- An outage removes the protection rather than the API, and clients can't
  tell. So the limiter exports two Prometheus metrics on `/metrics` (with
  `ENABLE_METRICS` on): `rate_limit_backend_degraded` (1 while failing open,
  0 otherwise) and `rate_limit_backend_errors_total{tier}` (failed Redis
  calls). Alert on these, not on the one-off log line:
  `max_over_time(rate_limit_backend_degraded[1m]) == 1` (the `max_over_time`
  keeps it firing through the brief 0 between one probe and the next), and/or
  `increase(rate_limit_backend_errors_total[5m]) > 0`. This matters because
  `/readyz` (3 s Redis timeout) can stay green while the limiter (250 ms)
  is already failing open — a slow but alive Redis, an auth or script error.
- The gauge is "degraded", not "up", on purpose: an unlabelled gauge is
  exported as 0 from import time, so 0 must mean healthy or every start and
  every `RATE_LIMIT_ENABLED=false` deployment would look like an outage.
  It does not tell you the limiter is *disabled*; that is a config concern.
- The gauge is computed when `/metrics` is scraped (1 exactly while the
  breaker is skipping Redis), not `set()` on failure/recovery. A `set(1)`
  only clears when a request arrives to notice the recovery, so an idle
  service (staging, night-time) would stay "degraded" for hours and page for
  nothing. The cost: after the cooldown ends it reads 0 until the next
  request probes Redis, which is accurate (nothing is being skipped) but
  means it is not a "Redis is down" signal on its own; the error counter is.
- `/metrics` is unauthenticated and on the API port. It now also shows whether
  the limiter is failing open, which an attacker could equally infer from the
  `X-RateLimit-*` headers disappearing, but it should still not be routed
  to the public internet: restrict it at the proxy / network policy.
- `prometheus-client` is now a direct dependency (it was already installed
  as a dependency of `prometheus-fastapi-instrumentator`, whose registry
  serves `/metrics`); no other ADR had ruled it out. The metrics use the
  default registry, which is right for the single-process deployment in the
  Dockerfile. With `uvicorn --workers N` each worker has its own gauge and
  `/metrics` shows whichever answered: switch to prometheus_client's
  multiprocess mode (`PROMETHEUS_MULTIPROC_DIR`) before doing that.
- The real-Redis path is covered by `tests/integration/test_rate_limit_backend.py`
  (skipped when no Redis is reachable); the rest of the suite uses in-memory
  storage.

## Amendment: the UPLOAD tier (files module, ADR 0014)
Chunk uploads are `PUT` requests, so the mutation rule would put them on WRITE (30/minute). A
multi-gigabyte file in 20 MiB chunks would then take minutes just to be throttled. The files module
therefore adds a fifth tier, `UPLOAD` (`RATE_LIMIT_UPLOAD`, default `600/minute`), used only by
`PUT /files/uploads/{id}/chunks/{index}`. The coverage test accepts `rl:upload` as a mutation tier for
that reason. Every other mutation keeps WRITE, and the failure mode it guards against is unchanged.
