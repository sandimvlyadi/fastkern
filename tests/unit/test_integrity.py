from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

# Importing the models registers their tables on Base.metadata, which the SQLite path reads.
from app.modules.roles.infrastructure import models as _roles_models  # noqa: F401
from app.modules.tenants.infrastructure import models as _tenants_models  # noqa: F401
from app.modules.users.infrastructure import models as _users_models  # noqa: F401
from app.shared.infrastructure.integrity import violated_unique_constraint


class _AsyncpgError(Exception):
    def __init__(self, sqlstate: str, constraint_name: str | None) -> None:
        super().__init__("driver error")
        self.sqlstate = sqlstate
        self.constraint_name = constraint_name


def _postgres_error(sqlstate: str, constraint_name: str | None) -> IntegrityError:
    # SQLAlchemy's asyncpg adapter raises its own IntegrityError `from` the asyncpg one.
    adapted = Exception("adapted")
    adapted.__cause__ = _AsyncpgError(sqlstate, constraint_name)
    return IntegrityError("INSERT ...", {}, adapted)


def _sqlite_error(message: str) -> IntegrityError:
    return IntegrityError("INSERT ...", {}, Exception(message))


def test_postgres_unique_violation_returns_the_constraint_name() -> None:
    exc = _postgres_error("23505", "uq_sys_users_tenant_email")
    assert violated_unique_constraint(exc) == "uq_sys_users_tenant_email"


@pytest.mark.parametrize(
    ("sqlstate", "name"),
    [("23503", "fk_something"), ("23502", None), ("23514", "ck_x"), ("23505", None)],
)
def test_postgres_non_unique_or_nameless_violation_is_not_reported(
    sqlstate: str, name: str | None
) -> None:
    assert violated_unique_constraint(_postgres_error(sqlstate, name)) is None


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        # partial unique index
        (
            "UNIQUE constraint failed: sys_users.tenant_id, sys_users.email",
            "uq_sys_users_tenant_email",
        ),
        # column order in the message is not significant
        (
            "UNIQUE constraint failed: sys_users.email, sys_users.tenant_id",
            "uq_sys_users_tenant_email",
        ),
        (
            "UNIQUE constraint failed: sys_roles.tenant_id, sys_roles.name",
            "uq_sys_roles_tenant_name",
        ),
        ("UNIQUE constraint failed: sys_tenants.slug", "uq_sys_tenants_slug"),
        # plain UNIQUE constraint (not an index)
        (
            "UNIQUE constraint failed: sys_user_roles.tenant_id, sys_user_roles.user_id, "
            "sys_user_roles.role_id",
            "uq_sys_user_roles_tenant_user_role",
        ),
    ],
)
def test_sqlite_unique_violation_is_resolved_from_the_orm_metadata(
    message: str, expected: str
) -> None:
    assert violated_unique_constraint(_sqlite_error(message)) == expected


@pytest.mark.parametrize(
    "message",
    [
        "NOT NULL constraint failed: sys_users.email",
        "FOREIGN KEY constraint failed",
        "UNIQUE constraint failed: no_such_table.a",
        "UNIQUE constraint failed: sys_users.full_name",  # no unique key over exactly these columns
    ],
)
def test_sqlite_other_integrity_errors_are_not_reported(message: str) -> None:
    assert violated_unique_constraint(_sqlite_error(message)) is None


def test_error_without_a_driver_cause_is_handled() -> None:
    orig: Any = ValueError("weird")
    assert violated_unique_constraint(IntegrityError("x", {}, orig)) is None
