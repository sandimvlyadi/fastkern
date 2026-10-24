"""Database naming / column standards, enforced for every ORM table.

A new module's models are picked up automatically (discovered from disk), so
forgetting a prefix, a comment or the column order fails `make test` — the
same way tests/unit/test_i18n_key_coverage.py guards the i18n catalog.

Each rule is a function `Table -> list[str]` (violations), so the rules
themselves are tested against deliberately bad tables at the bottom.
"""

import importlib
import re
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import (
    Column,
    ForeignKey,
    Index,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    UniqueConstraint,
    Uuid,
)

from app.shared.infrastructure.orm_base import Base

_MODULES_DIR = Path(__file__).resolve().parents[2] / "src" / "app" / "modules"

for _models_file in sorted(_MODULES_DIR.glob("*/infrastructure/models.py")):
    importlib.import_module(f"app.modules.{_models_file.parent.parent.name}.infrastructure.models")

TABLE_NAME = re.compile(r"^(sys|master|trans)_[a-z][a-z0-9]*(_[a-z0-9]+)*$")
ACTOR_COLUMNS = ("created_by", "updated_by", "deleted_by")
TIMESTAMP_COLUMNS = ("created_at", "updated_at", "deleted_at")

Rule = Callable[[Table], list[str]]


def _rank(column: Column[object]) -> tuple[int, int]:
    """Position bucket of a column: id, tenant, FKs/parents, main, actors, timestamps."""
    if column.name == "id":
        return (0, 0)
    if column.name == "tenant_id":
        return (1, 0)
    if column.foreign_keys:
        return (2, 0)
    if column.name in ACTOR_COLUMNS:
        return (4, ACTOR_COLUMNS.index(column.name))
    if column.name in TIMESTAMP_COLUMNS:
        return (5, TIMESTAMP_COLUMNS.index(column.name))
    return (3, 0)


def check_name_prefix(table: Table) -> list[str]:
    if TABLE_NAME.match(table.name):
        return []
    return [f"'{table.name}' must match {TABLE_NAME.pattern} (sys_ / master_ / trans_ prefix)"]


def check_column_order(table: Table) -> list[str]:
    columns = list(table.columns)
    if columns[0].name != "id" or not columns[0].primary_key:
        return [f"'{table.name}': first column must be the 'id' primary key"]
    ranks = [_rank(c) for c in columns]
    if ranks != sorted(ranks):
        actual = ", ".join(c.name for c in columns)
        return [
            f"'{table.name}': columns out of order ({actual}); expected id, tenant_id, "
            "parent/FK ids, main columns, created_by/updated_by/deleted_by, "
            "created_at/updated_at/deleted_at — use the ORDER_* constants in orm_base"
        ]
    return []


def check_comments(table: Table) -> list[str]:
    problems = [] if table.comment else [f"'{table.name}': table has no comment"]
    problems += [
        f"'{table.name}.{c.name}': column has no comment" for c in table.columns if not c.comment
    ]
    return problems


def check_server_defaults(table: Table) -> list[str]:
    return [
        f"'{table.name}.{c.name}': NOT NULL with a Python-side default needs a server_default too"
        for c in table.columns
        if not c.primary_key
        and not c.nullable
        and c.default is not None
        and c.server_default is None
    ]


def _where(index: Index) -> object:
    return index.dialect_options["postgresql"]["where"]


def check_soft_delete_uniqueness(table: Table) -> list[str]:
    if "deleted_at" not in table.c:
        return []
    problems = []
    if "deleted_by" not in table.c:
        problems.append(f"'{table.name}': has deleted_at but no deleted_by")
    for constraint in table.constraints:
        if isinstance(constraint, UniqueConstraint):
            problems.append(
                f"'{table.name}': unique constraint {constraint.name!r} on a soft-delete table "
                "must be a soft_delete_unique_index() (partial: WHERE deleted_at IS NULL)"
            )
    for index in table.indexes:
        if index.unique and _where(index) is None:
            problems.append(
                f"'{table.name}': unique index {index.name!r} on a soft-delete table "
                "must be a soft_delete_unique_index() (partial: WHERE deleted_at IS NULL)"
            )
    return problems


def _leading_columns(table: Table) -> set[str]:
    leading = {i.columns.keys()[0] for i in table.indexes if i.columns.keys()}
    # Only constraints that create an index: a ForeignKeyConstraint does not.
    for constraint in table.constraints:
        if isinstance(constraint, UniqueConstraint | PrimaryKeyConstraint):
            names = [c.name for c in constraint.columns]
            if names:
                leading.add(names[0])
    return leading


def check_index_coverage(table: Table) -> list[str]:
    """tenant_id must lead some index (a real access path, not a bare index),
    and every other FK column must lead one too (join / cascade lookups).

    A partial index counts: on a soft-delete table the composites are partial
    (`WHERE deleted_at IS NULL`), which serves every default repository query.
    It does NOT serve a query that includes deleted rows; a feature that needs
    that adds its own index.
    """
    problems = []
    leading = _leading_columns(table)
    if "tenant_id" in table.c and "tenant_id" not in leading:
        problems.append(
            f"'{table.name}': tenant_id leads no index/unique — add a composite led by "
            "tenant_id that matches the main query (e.g. (tenant_id, email))"
        )
    for column in table.columns:
        if column.foreign_keys and column.name != "tenant_id" and column.name not in leading:
            problems.append(f"'{table.name}.{column.name}': foreign key column is not indexed")
    return problems


RULES: dict[str, Rule] = {
    "name_prefix": check_name_prefix,
    "column_order": check_column_order,
    "comments": check_comments,
    "server_defaults": check_server_defaults,
    "soft_delete_uniqueness": check_soft_delete_uniqueness,
    "index_coverage": check_index_coverage,
}

_TABLES = sorted(Base.metadata.tables.values(), key=lambda t: t.name)


def test_schema_has_tables_to_check() -> None:
    assert len(_TABLES) >= 5


@pytest.mark.parametrize("rule", RULES)
@pytest.mark.parametrize("table", _TABLES, ids=lambda t: t.name)
def test_table_follows_standard(table: Table, rule: str) -> None:
    assert RULES[rule](table) == []


# --- the rules themselves: each must actually catch its violation -----------


def _bad_table(*columns: Column[object] | Index | UniqueConstraint, name: str = "users") -> Table:
    return Table(name, MetaData(), *columns)


def test_rule_name_prefix_rejects_unprefixed_and_unknown_prefix() -> None:
    assert check_name_prefix(_bad_table(Column("id", Uuid, primary_key=True)))
    assert check_name_prefix(_bad_table(Column("id", Uuid, primary_key=True), name="app_users"))
    assert not check_name_prefix(
        _bad_table(Column("id", Uuid, primary_key=True), name="trans_orders")
    )


def test_rule_column_order_rejects_id_last_and_actor_before_main() -> None:
    id_last = _bad_table(Column("name", String), Column("id", Uuid, primary_key=True))
    assert check_column_order(id_last)
    actor_early = _bad_table(
        Column("id", Uuid, primary_key=True), Column("created_by", Uuid), Column("name", String)
    )
    assert check_column_order(actor_early)
    ok = _bad_table(
        Column("id", Uuid, primary_key=True),
        Column("name", String),
        Column("created_by", Uuid),
        Column("created_at", String),
    )
    assert not check_column_order(ok)


def test_rule_column_order_wants_parent_fk_before_main_columns() -> None:
    metadata = MetaData()
    parent = Table("sys_parents", metadata, Column("id", Uuid, primary_key=True))
    late_fk = Table(
        "sys_children",
        metadata,
        Column("id", Uuid, primary_key=True),
        Column("name", String),
        Column("parent_id", ForeignKey(parent.c.id)),
    )
    assert check_column_order(late_fk)


def test_rule_comments_requires_table_and_column_comments() -> None:
    assert len(check_comments(_bad_table(Column("id", Uuid, primary_key=True)))) == 2


def test_rule_soft_delete_uniqueness_rejects_plain_unique_and_missing_deleted_by() -> None:
    table = _bad_table(
        Column("id", Uuid, primary_key=True),
        Column("email", String),
        Column("deleted_at", String),
        UniqueConstraint("email", name="uq_email"),
    )
    problems = check_soft_delete_uniqueness(table)
    assert any("no deleted_by" in p for p in problems)
    assert any("uq_email" in p for p in problems)


def test_rule_index_coverage_rejects_unindexed_tenant_id_and_fk() -> None:
    metadata = MetaData()
    tenants = Table("sys_tenants", metadata, Column("id", Uuid, primary_key=True))
    parents = Table("sys_parents", metadata, Column("id", Uuid, primary_key=True))
    table = Table(
        "sys_children",
        metadata,
        Column("id", Uuid, primary_key=True),
        Column("tenant_id", ForeignKey(tenants.c.id)),
        Column("parent_id", ForeignKey(parents.c.id)),
    )
    assert len(check_index_coverage(table)) == 2
