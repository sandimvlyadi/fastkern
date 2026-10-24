# 2. Modular monolith with Clean Architecture layering

## Status
Accepted

## Context
The project must serve as a base for many future products. Two extremes
were considered: (a) a single unlayered FastAPI app (fast to start, but
degrades into a "big ball of mud" as it grows), and (b) microservices
from day one (operational overhead — deployment, service discovery,
distributed transactions — that isn't justified before there's more than
one team or a proven scaling need).

## Decision
Build a **modular monolith**: one deployable, split into
`app/modules/<name>/{domain,application,infrastructure,presentation}`.
Each module's domain/application layers stay framework-agnostic; modules
talk to each other only through domain ports, wired in a single
composition root (`app/core/di.py`). This is enforced with
`import-linter`, not left to convention.

## Consequences
- A module can be extracted into its own service later with comparatively
  little rework, since its domain/application code never depended on
  FastAPI/SQLAlchemy or another module's internals.
- Local development, testing, and deployment stay as simple as a single
  process for as long as that's sufficient.
- The cost is discipline: every new cross-module dependency must go
  through a port + the composition root, which is more ceremony than
  just importing a sibling module's repository. This is intentional —
  see `CLAUDE.md` §2.
