"""Print the next sequential Alembic revision id (e.g. "0004").

Used by `make migration` to pass `--rev-id` automatically instead of
letting Alembic assign a random hex id. Reads only the on-disk script
graph (migrations/versions/) — never connects to a database, so it
works even with no DATABASE_URL configured.

    uv run python scripts/next_migration_id.py
"""

import re
import sys
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

# Must match tests/unit/test_migration_naming.py's `_SEQUENTIAL_ID` — both
# read the same revision graph and need to agree on what a valid id looks like.
_SEQUENTIAL_ID = re.compile(r"^\d{4,}$")


def main() -> int:
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).resolve().parent.parent / "migrations")
    )
    script = ScriptDirectory.from_config(config)

    heads = script.get_heads()
    if len(heads) > 1:
        print(
            f"Multiple Alembic heads found: {heads}. Run `alembic merge` "
            "before adding a new migration.",
            file=sys.stderr,
        )
        return 1

    numbered = [
        int(rev.revision) for rev in script.walk_revisions() if _SEQUENTIAL_ID.match(rev.revision)
    ]
    next_id = max(numbered, default=0) + 1
    print(f"{next_id:04d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
