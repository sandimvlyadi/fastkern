"""Postgres trigger that keeps `updated_at` honest.

`updated_at` also has an ORM-side `onupdate=func.now()`, but that only fires
for UPDATEs issued through SQLAlchemy's unit of work. A bulk
`UPDATE ... WHERE`, a data fix in psql, or an ETL job would leave it stale.
The trigger makes the database itself the source of truth; the ORM
`onupdate` stays so SQLite (the fast local test DB) behaves the same.

Two consumers share these statements so they cannot drift apart:
  * `Base.metadata.create_all()` (tests) — via the `after_create` listener
    installed at the bottom of `orm_base.py`;
  * Alembic migrations — run `updated_at_trigger_ddl()` after creating tables
    (`op.create_table` does not fire metadata events), and `DROP_FUNCTION` on
    downgrade.

Every statement is a separate `text()` (no bind parameters): asyncpg cannot run several commands in
one prepared statement.
"""

from collections.abc import Iterable
from typing import Any

from sqlalchemy import Connection, MetaData, Table, TextClause, event, text
from sqlalchemy.ext.asyncio import AsyncConnection

FUNCTION_NAME = "sys_set_updated_at"

CREATE_FUNCTION = text(
    f"""
    CREATE OR REPLACE FUNCTION {FUNCTION_NAME}() RETURNS trigger AS $$
    BEGIN
        NEW.updated_at = now();
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """
)
DROP_FUNCTION = text(f"DROP FUNCTION IF EXISTS {FUNCTION_NAME}()")


def trigger_name(table: str) -> str:
    return f"trg_{table}_set_updated_at"


def _create_trigger(table: str) -> tuple[TextClause, TextClause]:
    name = trigger_name(table)
    return (
        text(f"DROP TRIGGER IF EXISTS {name} ON {table}"),
        text(
            f"CREATE TRIGGER {name} BEFORE UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION {FUNCTION_NAME}()"
        ),
    )


def updated_at_trigger_ddl(tables: Iterable[str]) -> list[TextClause]:
    """Statements that (re)create the function and one trigger per table.

    Idempotent. Migrations run each with `op.execute()`; the `create_all()`
    listener below runs them on a connection.
    """
    statements = [CREATE_FUNCTION]
    for table in tables:
        statements.extend(_create_trigger(table))
    return statements


# Tables of the current schema that have an `updated_at` column but no enabled
# `trg_<table>_set_updated_at` trigger calling `sys_set_updated_at()`.
_MISSING_TRIGGERS = text(
    """
    SELECT t.table_name
    FROM information_schema.tables t
    JOIN information_schema.columns c
      ON c.table_schema = t.table_schema
     AND c.table_name = t.table_name
     AND c.column_name = 'updated_at'
    WHERE t.table_schema = current_schema()
      AND t.table_type = 'BASE TABLE'
      AND NOT EXISTS (
        SELECT 1
        FROM pg_trigger g
        JOIN pg_proc p ON p.oid = g.tgfoid AND p.proname = :function_name
        WHERE g.tgrelid = to_regclass(format('%I.%I', t.table_schema, t.table_name))
          AND g.tgname = 'trg_' || t.table_name || '_set_updated_at'
          AND g.tgenabled <> 'D'
          AND NOT g.tgisinternal
      )
    ORDER BY t.table_name
    """
).bindparams(function_name=FUNCTION_NAME)


async def tables_missing_updated_at_trigger(connection: AsyncConnection) -> list[str]:
    """Guard for migrations: `alembic check` cannot see triggers, and the test
    suite builds its schema with `create_all()` (which does add them), so a
    migration that creates a table with `updated_at` and forgets
    `updated_at_trigger_ddl()` would otherwise ship silently. Run against a
    database at `alembic upgrade head` by `scripts/check_updated_at_triggers.py`
    (CI's `test-postgres` job)."""
    result = await connection.execute(_MISSING_TRIGGERS)
    return [row[0] for row in result]


_FUNCTION_IN_USE = text(
    """
    SELECT count(*)
    FROM pg_trigger g
    JOIN pg_proc p ON p.oid = g.tgfoid AND p.proname = :function_name
    WHERE NOT g.tgisinternal
    """
).bindparams(function_name=FUNCTION_NAME)


def drop_updated_at_function_if_unused(connection: Connection) -> None:
    """Drop the function unless some table still has a trigger calling it.

    `drop_all(tables=[...])` may remove only some tables; an unconditional
    DROP FUNCTION would then fail with "other objects depend on it".
    """
    if connection.execute(_FUNCTION_IN_USE).scalar_one() == 0:
        connection.execute(DROP_FUNCTION)


def install(metadata: MetaData) -> None:
    """Attach the `after_create` listener that adds triggers on `create_all()`."""

    @event.listens_for(metadata, "after_create")
    def _add_triggers(target: MetaData, connection: Connection, **kw: Any) -> None:
        if connection.dialect.name != "postgresql":
            return
        # `tables` = the tables this create_all() call actually created (after
        # checkfirst): a partial `create_all(tables=[...])` must not try to
        # attach triggers to tables that do not exist.
        created: list[Table] = kw["tables"]
        names = [t.name for t in created if "updated_at" in t.c]
        if names:
            for statement in updated_at_trigger_ddl(names):
                connection.execute(statement)

    @event.listens_for(metadata, "after_drop")
    def _drop_function(target: MetaData, connection: Connection, **_: Any) -> None:
        if connection.dialect.name == "postgresql":
            drop_updated_at_function_if_unused(connection)
