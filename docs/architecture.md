# Architecture

## Why and when to use it

This base project is for teams that want:

- A production-oriented FastAPI foundation instead of starting from an empty project.
- Clear separation between business logic, infrastructure, and HTTP concerns.
- Multi-tenant APIs with RBAC and auditability built in.
- Consistent security, observability, error handling, and configuration practices.
- A modular structure that can evolve as the application grows.
- Architecture rules that are checked in CI instead of relying only on documentation.

## Modular monolith with Clean Architecture

The application is deployed as one service, but the codebase is divided into independent feature
modules. Each module has four layers:

```text
src/app/modules/<module>/
├── domain/            # entities, exceptions, ports
├── application/       # use cases and orchestration
├── infrastructure/    # persistence and external adapters
└── presentation/      # FastAPI routers and schemas
```

Dependencies point inward only:

```text
presentation → infrastructure → application → domain
```

The `domain` layer stays independent of FastAPI, SQLAlchemy, Redis, Celery, and other
infrastructure concerns.

Modules talk to each other through **domain ports**, never by importing another module's
infrastructure. Concrete implementations are wired together in one place, the composition root
`app/core/di.py`.

`import-linter` enforces these boundaries:

```bash
make arch-check
```

This keeps business logic independent of the framework and the database, so a module is easy to
test, refactor, or later extract into its own service.

## Modules

| Module | Purpose |
|---|---|
| `auth` | Register, login, refresh, logout, role switching |
| `users` | User profiles, activation, avatars |
| `roles` | RBAC roles, role assignment, the permission catalog |
| `tenants` | Platform-level tenant provisioning |
| `audit_log` | Append-only record of state-changing actions |
| `notifications` | Email delivery from the Celery worker |
| `files` | Resumable uploads, content detection, malware scanning, image variants |
| `api_clients` | Partner API keys: issue, rotate, revoke |
| `partners` | The read-only API partners call with an API key |
| `master_examples`, `examples` | Reference implementations of a master-data and a transactional module |
| `posts` | Reference implementation of an external HTTP API integration |

The example modules are meant to be read and copied; remove them in a project built on this base.

## Design principles

- Domain logic doesn't depend on frameworks.
- Application use cases orchestrate domain entities and ports, one class per use case.
- Infrastructure implements ports and integrations.
- Presentation translates HTTP into application calls and back, nothing more.
- Modules never couple through each other's infrastructure.
- Security and production requirements are enforced by code and CI where possible.
- Architectural decisions are written down as ADRs.

## Architecture decisions

Significant trade-offs are recorded as Architecture Decision Records in [`docs/adr/`](./adr/).
Before proposing an architectural change, check whether an ADR already covers it and follow its
constraints; add a new ADR for a new decision.

| ADR | Decision |
|---|---|
| [0001](./adr/0001-record-architecture-decisions.md) | Record architecture decisions |
| [0002](./adr/0002-modular-monolith-clean-architecture.md) | Modular monolith with Clean Architecture |
| [0003](./adr/0003-multi-tenancy-row-level.md) | Row-level multi-tenancy |
| [0004](./adr/0004-jwt-rbac-role-switching.md) | JWT, RBAC and role switching |
| [0005](./adr/0005-i18n-timezone-response-conventions.md) | i18n and timezone response conventions |
| [0006](./adr/0006-audit-log-snapshots-and-pii.md) | Audit log snapshots and PII |
| [0007](./adr/0007-rate-limiting.md) | Rate limiting |
| [0008](./adr/0008-observability-backend.md) | Observability backend |
| [0009](./adr/0009-database-naming-and-column-standards.md) | Database naming and column standards |
| [0010](./adr/0010-session-revocation.md) | Session revocation |
| [0011](./adr/0011-notification-delivery.md) | Notification delivery |
| [0012](./adr/0012-tenant-provisioning.md) | Tenant provisioning |
| [0013](./adr/0013-migration-revision-naming.md) | Migration revision naming |
| [0014](./adr/0014-file-storage-and-resumable-uploads.md) | File storage and resumable uploads |
| [0015](./adr/0015-image-pipeline-and-public-file-serving.md) | Image pipeline and public file serving |
| [0016](./adr/0016-ops-heartbeat-and-readiness-scope.md) | Ops heartbeat and readiness scope |
| [0017](./adr/0017-user-avatar-via-files-module.md) | User avatars through the files module |
| [0018](./adr/0018-release-process.md) | Release process |
| [0019](./adr/0019-external-api-integration-pattern.md) | External API integration pattern |
| [0020](./adr/0020-partner-api-key-integration.md) | Partner API key integration |
| [0021](./adr/0021-permission-and-scope-catalog-in-code.md) | Permission and scope catalog in code |
| [0022](./adr/0022-container-image-publishing.md) | Container image publishing |
| [0023](./adr/0023-single-host-deployment.md) | Reference deployment: one host with Compose and nginx |
| [0024](./adr/0024-documentation-site-and-quickstart.md) | Documentation site and a quickstart from the published image |
