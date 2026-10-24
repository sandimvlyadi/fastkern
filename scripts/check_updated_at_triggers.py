"""Fail if any table with an `updated_at` column lacks its updated_at trigger.

Run against a database that is at `alembic upgrade head` (CI does, right
after `alembic check`). Autogenerate cannot see triggers, so this is the only
thing that catches a migration which forgot `updated_at_trigger_ddl()`.

    uv run python scripts/check_updated_at_triggers.py
"""

import asyncio
import sys

from app.core.config import get_settings
from app.core.db import create_engine
from app.shared.infrastructure.pg_triggers import tables_missing_updated_at_trigger


async def main() -> int:
    engine = create_engine(get_settings(), pooled=False)
    try:
        async with engine.connect() as connection:
            if connection.dialect.name != "postgresql":
                print("Skipped: updated_at triggers exist on Postgres only.")
                return 0
            missing = await tables_missing_updated_at_trigger(connection)
    finally:
        await engine.dispose()

    if missing:
        print(
            "Tables with `updated_at` but no updated_at trigger: "
            + ", ".join(missing)
            + "\nAdd `for s in updated_at_trigger_ddl((...)): op.execute(s)` to the "
            "migration that creates them (migrations/versions/0001_baseline_schema.py).",
            file=sys.stderr,
        )
        return 1
    print("OK: every table with updated_at has its trigger.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
