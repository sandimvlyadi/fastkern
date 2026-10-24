# Observability

- Structured JSON logs, each line carrying `request_id`, `tenant_id`, `user_id` and the trace id
- OpenTelemetry tracing
- Prometheus metrics at `/metrics`
- Health (`/healthz`) and readiness (`/readyz`) endpoints

`/metrics` lists every route and its traffic. Protect it in production with `METRICS_AUTH_TOKEN`
(clients then send `Authorization: Bearer <token>`) or keep it off the public listener at the
reverse proxy; production logs `metrics_endpoint_unauthenticated` at startup while the token is
unset.

## Readiness and the ops heartbeat

`/readyz` returns `503` only when a check the API can't serve without fails: `database`, `redis`
or `celery` (the broker). The response also reports the background services:

| Check | Source | Gates `/readyz`? |
|---|---|---|
| `beat` | `ops.heartbeat` ran in the last 3 × `OPS_HEARTBEAT_INTERVAL_SECONDS`, so beat, the broker and a worker all work | No |
| `clamav` | clamd scanned a harmless sample as clean in the last heartbeat; present only when `MALWARE_SCAN_BACKEND=clamav` | No |

A dead beat or ClamAV doesn't take the API out of rotation. Alert on the metrics instead.

The API and the worker must use the same `MALWARE_SCAN_BACKEND`: the worker writes the ClamAV
status from its own setting, so a mismatch shows as `clamav: error` indefinitely. Uploads sent
while ClamAV is down are marked `FAILED` (`SCAN_UNAVAILABLE`), never `AVAILABLE`.

See [ADR 0016](./adr/0016-ops-heartbeat-and-readiness-scope.md).

## Metrics worth alerting on

| Metric | Meaning |
|---|---|
| `ops_check_up{check="beat"}` | `0`: no recent heartbeat, so periodic jobs aren't running |
| `ops_check_up{check="clamav"}` | `0`: clamd unreachable; `NaN` when scanning is off |
| `rate_limit_backend_degraded` | `1` while requests are served without rate limits because Redis is unreachable |
| `rate_limit_backend_errors_total{tier}` | Failed Redis calls from the rate limiter |
| `session_denylist_backend_errors_total` | Failed Redis calls from the session denylist (logout may not take effect) |

See [ADR 0008](./adr/0008-observability-backend.md).
