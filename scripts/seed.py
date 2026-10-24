#!/usr/bin/env python3
"""Seed baseline data: 1 tenant, 4 roles (developer, superadmin, admin,
user), 1 user per role.

Idempotent: safe to run repeatedly — each entity is looked up before being
created, never duplicated.

In staging/production it refuses to run without an explicit
`SEED_DEFAULT_PASSWORD` (>= 12 chars, not the public dev default) and
`SEED_DEVELOPER_EMAIL`: the dev fallbacks below are public.

Usage: uv run python scripts/seed.py (or `make seed`; in Docker:
`docker compose --profile tools run --rm seed`)
"""

import asyncio
import os
from uuid import UUID

from sqlalchemy import select

from app.core.config import Environment, get_settings
from app.core.db import get_session_factory
from app.core.security import hash_password
from app.modules.files.infrastructure import models as _files_models  # noqa: F401
from app.modules.roles.domain.baseline import PLATFORM_ROLE_PERMISSIONS, TENANT_ROLE_PERMISSIONS
from app.modules.roles.infrastructure.models import RoleModel, UserRoleModel
from app.modules.tenants.infrastructure.models import TenantModel
from app.modules.users.domain.email import normalize_email
from app.modules.users.infrastructure.models import UserModel

# `_files_models` is imported only so `sys_users.avatar_file_id`'s FK to `trans_files.id` can
# resolve at SQLAlchemy mapper-configuration time: this script runs standalone (not through
# `app.main`/`core/di.py`, which already import every module's models), so without this import
# `trans_files` is never registered on `Base.metadata` and the first flush of a `UserModel` row
# fails with `NoReferencedTableError`.

DEFAULT_SEED_PASSWORD = "ChangePlease123!"  # noqa: S105 - local/test-only fallback, see docs/production.md
LOCAL_DEVELOPER_EMAIL = "developer@example.com"
MIN_DEPLOYED_PASSWORD_LENGTH = 12

TENANT_SLUG = "default"
TENANT_NAME = "Default Tenant"

# Only `developer`/`superadmin` hold `*`; the tenant-level roles come from the same definitions
# tenant provisioning uses, so a seeded tenant and a freshly created one match.
ROLE_PERMISSIONS: dict[str, list[str]] = {
    **{name: list(perms) for name, perms in PLATFORM_ROLE_PERMISSIONS.items()},
    **{name: list(perms) for name, perms in TENANT_ROLE_PERMISSIONS.items()},
}


class SeedConfigError(ValueError):
    """The seed was asked to run with credentials that are unsafe for this environment."""


def resolve_seed_credentials(
    environment: Environment, password: str | None, developer_email: str | None
) -> tuple[str, str]:
    """Returns `(password, developer_email)` to seed with, or raises `SeedConfigError`.

    Local/test fall back to the public dev defaults so `make seed` keeps working out of the box.
    Staging/production must be told both explicitly: a seeded account with a publicly known
    password (or someone's personal address) is a backdoor.
    """
    email = (developer_email or "").strip().lower()
    if environment not in (Environment.STAGING, Environment.PRODUCTION):
        return password or DEFAULT_SEED_PASSWORD, email or LOCAL_DEVELOPER_EMAIL

    problems: list[str] = []
    if not password:
        problems.append("SEED_DEFAULT_PASSWORD is required")
    elif password == DEFAULT_SEED_PASSWORD:
        problems.append("SEED_DEFAULT_PASSWORD must not be the public default")
    elif len(password) < MIN_DEPLOYED_PASSWORD_LENGTH:
        problems.append(
            f"SEED_DEFAULT_PASSWORD must be at least {MIN_DEPLOYED_PASSWORD_LENGTH} characters"
        )
    if not email or "@" not in email:
        problems.append("SEED_DEVELOPER_EMAIL is required (a real address)")
    if problems:
        raise SeedConfigError(
            f"Refusing to seed ENVIRONMENT={environment.value}: " + "; ".join(problems) + "."
        )
    return password or "", email


def build_role_users(developer_email: str) -> dict[str, tuple[str, str]]:
    """role_name -> (full_name, email)."""
    return {
        "developer": ("Developer", developer_email),
        "superadmin": ("Super Admin", "superadmin@example.com"),
        "admin": ("Admin", "admin@example.com"),
        "user": ("Default User", "user@example.com"),
    }


async def _get_or_create_tenant(session, tenant_id: UUID) -> tuple[TenantModel, bool]:
    existing = (
        await session.execute(select(TenantModel).where(TenantModel.slug == TENANT_SLUG))
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False
    tenant = TenantModel(id=tenant_id, name=TENANT_NAME, slug=TENANT_SLUG)
    session.add(tenant)
    await session.flush()
    return tenant, True


async def _get_or_create_role(
    session, *, tenant_id: UUID, name: str, permissions: list[str]
) -> tuple[RoleModel, bool]:
    existing = (
        await session.execute(
            select(RoleModel).where(RoleModel.tenant_id == tenant_id, RoleModel.name == name)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False
    role = RoleModel(tenant_id=tenant_id, name=name, permissions=permissions, is_system=True)
    session.add(role)
    await session.flush()
    return role, True


async def _get_or_create_user(
    session, *, tenant_id: UUID, email: str, full_name: str, hashed_password: str
) -> tuple[UserModel, bool]:
    email = normalize_email(email)  # sys_users refuses anything else (ck_sys_users_email_lower)
    existing = (
        await session.execute(
            select(UserModel).where(UserModel.tenant_id == tenant_id, UserModel.email == email)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False
    user = UserModel(
        tenant_id=tenant_id, email=email, full_name=full_name, hashed_password=hashed_password
    )
    session.add(user)
    await session.flush()
    return user, True


async def _get_or_create_assignment(
    session, *, tenant_id: UUID, user_id: UUID, role_id: UUID
) -> bool:
    existing = (
        await session.execute(
            select(UserRoleModel).where(
                UserRoleModel.tenant_id == tenant_id,
                UserRoleModel.user_id == user_id,
                UserRoleModel.role_id == role_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return False
    session.add(UserRoleModel(tenant_id=tenant_id, user_id=user_id, role_id=role_id))
    await session.flush()
    return True


async def main() -> None:
    settings = get_settings()
    tenant_id = UUID(settings.default_tenant_id)

    try:
        seed_password, developer_email = resolve_seed_credentials(
            settings.environment,
            os.environ.get("SEED_DEFAULT_PASSWORD"),
            os.environ.get("SEED_DEVELOPER_EMAIL"),
        )
    except SeedConfigError as exc:
        raise SystemExit(f"seed: {exc}") from exc
    password_source = "SEED_DEFAULT_PASSWORD" if os.environ.get("SEED_DEFAULT_PASSWORD") else None
    role_users = build_role_users(developer_email)
    hashed_password = hash_password(seed_password)

    created = {"tenant": 0, "roles": 0, "users": 0, "assignments": 0}

    session_factory = get_session_factory()
    async with session_factory() as session:
        tenant, tenant_created = await _get_or_create_tenant(session, tenant_id)
        created["tenant"] += int(tenant_created)

        for role_name, permissions in ROLE_PERMISSIONS.items():
            role, role_created = await _get_or_create_role(
                session, tenant_id=tenant.id, name=role_name, permissions=permissions
            )
            created["roles"] += int(role_created)

            full_name, email = role_users[role_name]
            user, user_created = await _get_or_create_user(
                session,
                tenant_id=tenant.id,
                email=email,
                full_name=full_name,
                hashed_password=hashed_password,
            )
            created["users"] += int(user_created)

            assignment_created = await _get_or_create_assignment(
                session, tenant_id=tenant.id, user_id=user.id, role_id=role.id
            )
            created["assignments"] += int(assignment_created)

        await session.commit()

    print(f"Tenant '{TENANT_SLUG}': {'created' if created['tenant'] else 'already existed'}")
    print(f"Roles created: {created['roles']} (of {len(ROLE_PERMISSIONS)})")
    print(f"Users created: {created['users']} (of {len(role_users)})")
    print(f"Role assignments created: {created['assignments']} (of {len(role_users)})")
    if password_source:
        print(f"Seed password read from ${password_source}.")
    else:
        print(
            "Seed password used the built-in dev fallback (set SEED_DEFAULT_PASSWORD to override)."
        )


if __name__ == "__main__":
    asyncio.run(main())
