# 8. Observability backend (metrics + traces)

## Status
**Proposed — draft, not yet implemented.** Nothing in this ADR is binding
until the status becomes Accepted. Every item under *Decision* is a
starting position that may be corrected; see *Open questions* and
*Revision log* at the bottom. If reality (a benchmark, an ops constraint,
a wrong assumption below) disagrees with this text, change the text and
add a line to the revision log rather than working around it.

## Context
What exists today (verified against `main` at the time of writing):

- **Metrics**: `app/core/observability.py` exposes `/metrics` through
  `prometheus-fastapi-instrumentator` (`ENABLE_METRICS`, default `true`).
  ADR 0007 adds two custom metrics from the rate limiter
  (`rate_limit_backend_degraded`, `rate_limit_backend_errors_total{tier}`)
  and suggests alert expressions for them.
- **Tracing**: the same module builds a `TracerProvider` with an OTLP/HTTP
  exporter to `{OTEL_EXPORTER_OTLP_ENDPOINT}/v1/traces`, and instruments
  FastAPI and SQLAlchemy. `ENABLE_TRACING` defaults to `false`, and the
  default endpoint is `http://localhost:4318`.
- **Log correlation**: `app/core/logging.py` adds `trace_id`/`span_id` to
  each log line when a span is active.

What does not exist:

- No receiving side. `docker-compose.yml` has only `api`, `worker`, `db`
  and `redis`: no Prometheus, no OTel Collector, no trace store, no UI. With
  tracing on, spans go to a `localhost:4318` nobody listens on (and inside a
  container `localhost` is the container itself).
- No scrape config, dashboards or alert rules. The alerts ADR 0007
  recommends live only in prose.
- The Celery worker is not instrumented, and trace context is not
  propagated from the API into tasks, so a trace ends at the HTTP request
  even when the work continues in `worker`.
- No document says which backend this project targets. The env default
  `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318` is only the standard
  OTLP/HTTP port; it names no specific tool or UI, and nothing in the repo
  implies one was chosen.

## Decision
The direction below is **tentative**. Where a choice is genuinely open it
is listed under *Open questions* instead.

1. **The application only speaks open protocols.** It exposes Prometheus
   text format on `/metrics` and sends OTLP for traces. It never imports a
   vendor SDK, so the backend can be swapped without touching `app/`. This
   is already true and stays true.
2. **An OpenTelemetry Collector sits between the app and any trace
   backend** (`app -> collector -> backend`). The app's exporter endpoint
   points at the collector, so choosing or changing a backend is a
   collector config change, not an application change.
3. **Prometheus scrapes `/metrics`**; the app does not push metrics.
   Metrics stay on the Prometheus client (not migrated to OTel metrics),
   because ADR 0007's metrics and the alerts built on them already use it.
4. **One UI for exploration.** Proposal: Grafana, with Prometheus as the
   metrics data source and a trace store as the traces data source, so a
   `trace_id` from a log line can be pasted straight into a trace search.
   The trace store itself (Tempo vs Jaeger) is an open question.
5. **Local/dev stack is opt-in.** The backend services go behind a Compose
   profile (working name `observability`) in `docker-compose.yml`, so a
   plain `docker compose up` keeps starting only what it starts today.
   The API keeps `ENABLE_TRACING=false` by default; the profile's
   documentation says which env vars to set (in containers the endpoint is
   the collector's service name, not `localhost`).
6. **Config files live in the repo** (working name `deploy/observability/`:
   Prometheus scrape config, alert rules, Collector config, Grafana
   provisioning). ADR 0007's two alert expressions become real rules
   there, and the docs point at the rules instead of repeating them.
7. **Worker coverage is in scope, in a later step.** Instrument Celery and
   propagate trace context from the API into tasks so a request and the
   task it enqueued share one trace. Metrics for the worker need their own
   scrape target (it has no HTTP port today). See the dependency note below.

Suggested order, each step independently useful and independently
revertible: (a) compose profile + Prometheus + scrape config; (b)
alert rules from ADR 0007; (c) Collector + trace store + Grafana; (d)
Celery instrumentation and propagation; (e) production guidance.

## Consequences
- **Positive**: swapping the trace backend never touches application code;
  the default developer workflow is unchanged; the alerts ADR 0007 relies
  on become versioned, reviewable files instead of prose.
- **Dependency rule (CLAUDE.md section 6)**: step (d) likely needs
  `opentelemetry-instrumentation-celery`, a new top-level dependency.
  No existing ADR rules it out, but it must be confirmed at that point.
- **Operational cost**: three to four more containers in the opt-in
  profile, and configuration to maintain. Rejecting the whole thing (keep
  only the endpoints) stays a legitimate outcome; see *Open questions*.
- **Security**: `/metrics` is unauthenticated on the API port (ADR 0007).
  A Prometheus in the same Compose network can scrape it without exposing
  it, but nothing in this ADR makes `/metrics` safe to publish; production
  must restrict it at the proxy / network policy.
- **Multi-worker caveat inherited from ADR 0007**: the metrics use the
  default registry, right for the current single-process Dockerfile. Any
  `uvicorn --workers N` deployment needs prometheus_client multiprocess
  mode first, otherwise scrapes see one arbitrary worker's values.
- **Sampling and volume**: exporting every span is fine locally and may not
  be in production. This ADR sets no sampling policy; that needs real
  traffic numbers.
- **Superseding**: if a decision here is reversed, add ADR 0009 (or later)
  that supersedes this one and change this file's status to
  `Superseded by 00NN`. Small corrections while it is still Proposed go in
  the revision log instead.

## Open questions
Each one should be closed (with the answer written into *Decision*) before
the status moves to Accepted.

1. **Trace store**: Tempo (pairs with Grafana, object-storage friendly,
   more moving parts) or Jaeger all-in-one (one container, traces only,
   simplest for local use)? The proposal above leans Grafana + Tempo but has
   not compared operating cost.
2. ~~**Is "otel explorer" one of these?**~~ **Resolved (2026-09-26)**: no.
   The phrase came from a misreading of the OTLP endpoint in `.env`; no
   specific explorer tool was intended. Decision 4 is therefore a free
   design choice, not a requirement to match an existing tool.
3. **Scope of the Compose profile**: dev-only convenience, or also the
   reference for production? The draft assumes dev/reference only; real
   production topology (managed backend, Kubernetes) is not decided here.
4. **Should the Collector be mandatory?** It adds a hop. If only one
   backend will ever be used, exporting directly from the app is simpler;
   the draft prefers the Collector for swappability and tail sampling later.
5. **Worker metrics**: separate HTTP exporter in the worker process,
   Celery-specific exporter, or skip until needed?
6. **Logs**: out of scope here (only the `trace_id` correlation exists). Is
   a log backend wanted, and should it be a separate ADR?
7. **Alert delivery**: rules without an Alertmanager (or equivalent) only
   show up in the Prometheus UI. Who receives alerts, and by what channel?

## Revision log
- 2026-09-26: Initial draft, status Proposed. Written from a read-only
  survey of the repo; no implementation exists yet.
- 2026-09-26: Closed open question 2 and reworded the last Context bullet:
  "otel explorer" was never a project requirement, only a misreading of
  `OTEL_EXPORTER_OTLP_ENDPOINT`. No Decision item changed.
