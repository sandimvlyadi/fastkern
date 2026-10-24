"""Working out *which* unique constraint a failed INSERT/UPDATE violated.

Repositories use this to turn the race a check-then-insert leaves open (two requests both
see "name is free", both insert) into the same domain error the check would have raised,
instead of a 500. Postgres names the constraint; SQLite (the test database) only lists the
columns, so there the name is recovered from the ORM metadata.
"""

import re

from sqlalchemy import Index, UniqueConstraint
from sqlalchemy.exc import IntegrityError

from app.shared.infrastructure.orm_base import Base

_PG_UNIQUE_VIOLATION = "23505"
_SQLITE_UNIQUE = re.compile(r"^UNIQUE constraint failed: (?P<columns>.+)$")


def violated_unique_constraint(exc: IntegrityError) -> str | None:
    """Name of the unique constraint/index `exc` violated, or None if it was some other
    integrity error (FK, NOT NULL, CHECK, ...) or the name can't be determined."""
    orig = exc.orig
    # SQLAlchemy's asyncpg adapter re-raises asyncpg's exception as `__cause__`.
    driver_error = getattr(orig, "__cause__", None) or orig

    if getattr(driver_error, "sqlstate", None) == _PG_UNIQUE_VIOLATION:
        name = getattr(driver_error, "constraint_name", None)
        return str(name) if name else None

    match = _SQLITE_UNIQUE.match(str(orig))
    if match is None:
        return None
    qualified = {column.strip() for column in match["columns"].split(",")}
    table_name = next(iter(qualified)).split(".", 1)[0]
    table = Base.metadata.tables.get(table_name)
    if table is None:
        return None
    failed_columns = {column.split(".", 1)[1] for column in qualified}
    unique_keys: list[UniqueConstraint | Index] = [
        *(c for c in table.constraints if isinstance(c, UniqueConstraint)),
        *(i for i in table.indexes if i.unique),
    ]
    for key in unique_keys:
        if key.name is not None and {c.name for c in key.columns} == failed_columns:
            return str(key.name)
    return None
