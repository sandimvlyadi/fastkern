"""Alembic revision ids must be a contiguous sequential series with one head.

Catches three real
failure modes: a migration left with Alembic's default random hex id
(e.g. someone ran `alembic revision --autogenerate` directly instead of
`make migration` — exactly how the early `22e1985ac266`/`29d42280cac1`
ids were created, before this convention existed); two branches each adding "the next" migration and
landing on the same number (or forgetting to renumber after a rebase);
and two branches each adding a migration off the same parent without an
`alembic merge` (a second head Alembic would otherwise apply silently in
whichever order `alembic upgrade head` happens to pick).
"""

import re
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

# Must match scripts/next_migration_id.py's `_SEQUENTIAL_ID` — both read the
# same revision graph and need to agree on what a valid id looks like.
_SEQUENTIAL_ID = re.compile(r"^\d{4,}$")


def _script_directory() -> ScriptDirectory:
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[2] / "migrations")
    )
    return ScriptDirectory.from_config(config)


def test_every_revision_id_is_sequential() -> None:
    script = _script_directory()
    non_sequential = [
        rev.revision for rev in script.walk_revisions() if not _SEQUENTIAL_ID.match(rev.revision)
    ]
    assert not non_sequential, (
        f"Migration(s) with a non-sequential revision id: {non_sequential}. Every migration "
        "must be created via `make migration` (never `alembic revision` directly), which "
        "assigns the next sequential id instead of Alembic's default random hex."
    )


def test_migration_ids_are_contiguous_with_no_duplicates() -> None:
    script = _script_directory()
    numbered = sorted(
        int(rev.revision) for rev in script.walk_revisions() if _SEQUENTIAL_ID.match(rev.revision)
    )
    assert numbered == list(range(1, len(numbered) + 1)), (
        f"Migration revision ids must be contiguous starting at 0001, got {numbered}."
    )


def test_migration_filename_matches_its_revision_id() -> None:
    script = _script_directory()
    for rev in script.walk_revisions():
        if _SEQUENTIAL_ID.match(rev.revision):
            assert Path(rev.path).name.startswith(f"{rev.revision}_"), (
                f"{rev.path} filename must start with its own revision id ({rev.revision})."
            )


def test_migrations_have_a_single_head() -> None:
    script = _script_directory()
    heads = script.get_heads()
    assert len(heads) == 1, (
        f"Expected exactly one Alembic head, found {heads}. Run `alembic merge` to "
        "combine them before merging to main."
    )
