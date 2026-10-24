# 13. Migration revision naming

## Status
Accepted

## Context
Alembic assigns a random 12-character hex string as a migration's
`revision` id unless `--rev-id` is passed explicitly. Only the baseline
migration (`migrations/versions/0001_baseline_schema.py`) was hand-named
`0001`; the two migrations added since it (`add_master_examples_table`,
`add_trans_examples_tables`) drifted back to Alembic's default and ended
up as `22e1985ac266` and `29d42280cac1`. Nothing enforced the convention,
so a random hash is what a plain `make migration` produces every time,
and nothing catches two migrations landing on the same slot (two branches
each adding "the next" migration) or two migrations branching off the
same parent without an `alembic merge` (a second head).

## Decision
- A migration's Alembic **`revision` id itself** — not just its filename —
  is a zero-padded sequential integer starting at `0001`
  (`0001`, `0002`, `0003`, ...), always assigned via `--rev-id`, never
  left to Alembic's random default.
- `make migration m="..."` computes and passes the next id automatically,
  via `scripts/next_migration_id.py` (reads only the on-disk Alembic
  script graph, no DB connection needed). Nobody passes `--rev-id` by
  hand in the normal case.
- `tests/unit/test_migration_naming.py` fails `make test` — and therefore
  both CI's `test` and `test-postgres` jobs — if the sequence has a gap,
  a duplicate, a filename/id mismatch, or the script graph has more than
  one head.
- Migrations `22e1985ac266` and `29d42280cac1` were renumbered to `0002`
  and `0003` as part of adopting this ADR. This is a one-time, destructive
  rename of the `revision` string, done because nothing outside
  `migrations/versions/` referenced those ids (verified by a repo-wide
  search) and no shared/long-lived environment had applied them to a
  database that would outlive this change. **A migration is never
  renumbered again once it has reached `main`** — see
  [ADR 0009](0009-database-naming-and-column-standards.md)'s equivalent
  rule for `0001` ("the first deployment freezes `0001`"); the same
  reasoning applies to every migration after it: renumbering one that any
  environment has already applied leaves that environment's
  `alembic_version` pointing at a revision id that no longer exists.
- Accepted limitation: ids print with at least 4 digits
  (`f"{next_id:04d}"`) and grow naturally past `9999` if this project ever
  has that many migrations — no special handling is planned for that
  scale.

## Consequences
- Migration history reads in order (`0001`, `0002`, `0003`, ...) instead
  of unordered random hashes.
- `make migration` has one more moving part (`scripts/next_migration_id.py`),
  but it needs no environment/DB configuration and fails loudly (refuses
  to print an id) if the script graph already has more than one head,
  rather than silently assigning a colliding or branching id.
- A migration authored on a long-lived branch may need a quick rebase
  (re-run `make migration`'s id computation after merging/rebasing onto
  the latest `main`) if another migration landed first — the same
  trade-off any sequential-numbering scheme makes, and the gate test
  catches it before it reaches `main` either way.
