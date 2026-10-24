# 12. Tenant provisioning and platform-only tenant management

## Status
Accepted. Refines [ADR 0003](0003-multi-tenancy-row-level.md) and
[ADR 0004](0004-jwt-rbac-role-switching.md). **Contains a known gap** (see Consequences).

## Context
Two problems surfaced when the tenants module was exercised end to end:

1. **A tenant `admin` could manage every tenant.** The seed gave `admin` the `tenants:manage`
   permission, but the tenants repository is deliberately *not* tenant-scoped
   (`tenant_scoped=False`) — the module is the platform's own surface. So an admin of tenant A
   could list all tenants, rename tenant B to "HACKED" and create new tenants.
2. **A new tenant had no roles at all.** Users registering into it got no role
   (`default_role_missing` in the log), and nothing existed to assign, so a tenant created
   through the API could not be used without database surgery.

## Decision
- **`tenants:manage` and `tenants:delete` are platform-only.** Only the `*` roles (`developer`,
  `superadmin`) hold them. `tenant admin` and `user` never do, and `*` is never given to a
  tenant-level role. The permission-escalation guard already stops an admin minting a custom
  role that carries `tenants:*`.
- **One source of truth for baseline roles**: `app/modules/roles/domain/baseline.py` defines
  `PLATFORM_ROLE_PERMISSIONS` (`developer`, `superadmin`) and `TENANT_ROLE_PERMISSIONS`
  (`admin`, `user`). The seed script and tenant provisioning both read it, so a seeded tenant
  and a freshly created one start out identical. (The same module owns the permission-string
  format `resource:action` | `resource:*` | `*` used to validate role requests.)
- **Provisioning happens when a tenant is created**, in the same request and DB transaction:
  `CreateTenantUseCase` calls a `TenantRoleProvisionerPort` (tenants domain), implemented in
  `app/core/di.py` — the composition root, the only place allowed to know both modules — by
  the roles module's `ProvisionBaselineRolesUseCase`. A tenant therefore never exists without
  its `admin` and `user` system roles, and self-registration into it lands on `user`.
- **Migration policy**: the CHECK constraint that makes emails lowercase
  (`ck_sys_users_email_lower`, ADR 0009) was added to migration `0001` in place, not as a new
  revision, because the project has no deployment yet. Recreate local databases with
  `docker compose down -v`. Once there is a first deployment, `0001` is frozen.

## Consequences
- The seeded `admin` can no longer call any `/tenants` endpoint; existing local databases seeded
  with the old permission list must be recreated (or the role edited by hand).
- **Known gap — bootstrapping the first admin of a new tenant.** Provisioning gives the new
  tenant an `admin` *role*, but no way to assign it through the API: `POST /roles/users/{id}`
  resolves the role inside the **caller's own tenant**, so a platform superadmin (tenant A)
  cannot assign tenant B's `admin` role, and nobody in tenant B holds `roles:write` yet. Until
  this is solved, the first admin of a new tenant has to be set up out of band (a one-off
  script or SQL). Candidate follow-ups, in rough order of preference: a
  `scripts/bootstrap_tenant_admin.py` that creates the user and assigns `admin`; or optional
  `adminEmail`/`adminPassword` on `CreateTenantRequest` (rejected for now: it puts credentials
  in a platform API call). This ADR should be superseded when one of them lands.
