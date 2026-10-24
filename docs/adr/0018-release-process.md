# 18. Release process

## Status
Accepted

## Context
Through v0.1.0 and v0.2.0, cutting a release has meant the same ad-hoc sequence each time:
scope what merged since the last tag, clean up any comments that leaked internal references
(ADR numbers, PR numbers) instead of stating the underlying constraint, bump the version,
stand up the stack the way an operator actually would, manually exercise the API surface,
write the changelog, and ship through a branch/PR/merge/tag cycle. None of that was written
down, so each release re-derived the steps from scratch — a risk for a human doing it under
time pressure, and a bigger risk for an AI agent asked to "just release it" with no record of
what "it" has historically included (e.g. it would be easy to skip the production-mode QA pass
and only run `make test`, which exercises SQLite/mocked boundaries, not the real Docker image).

## Decision
A release follows this checklist, in order:

1. **Scope the release.** Diff `v<PREV>..HEAD` for merged PRs/commits (`git log v<PREV>..HEAD
   --oneline`, `git diff v<PREV>..HEAD --stat`) to know what actually shipped.
2. **Code hygiene pass.** No comment anywhere may reference an ADR number, a PR number, or an
   issue number — state the underlying constraint in plain language instead (see CLAUDE.md's
   comment conventions). Collapse any comment block that has grown past a short WHY into
   something terser. This runs every release, not just when something looks obviously messy,
   because comments drift back toward referencing their originating ADR as new code is written
   alongside existing ADR-heavy files.
3. **Version bump.** Update `pyproject.toml`'s `version` and
   `src/app/core/config.py`'s `app_version` default. Also check `.env` / `.env.example` for a
   pinned `APP_VERSION` — if present, it overrides the code default and must be bumped too, or
   the running API will keep reporting the old version in `/openapi.json` and `/healthz`.
4. **Static validation.** `make lint`, `make typecheck`, `make arch-check`, `make test`,
   `make precommit` all green, run against the dev dependency stack
   (`docker compose up -d db redis mailpit`).
5. **Production-mode QA.** Bring the full stack up the way an operator would —
   `make docker-up` (the plain `docker-compose.yml`, never the `docker-compose.dev.yml` hot-reload
   overlay) — then `make docker-seed`. Build the endpoint matrix from the running stack's
   `/openapi.json`, not from memory of the router files, since that is the one source of truth
   for "every endpoint currently exposed." Give full-depth testing (happy path + auth/permission
   failure + validation failure) to every endpoint whose router or schema changed since the
   previous tag (`git diff v<PREV>..HEAD --stat -- '**/presentation/router.py'
   '**/presentation/schemas.py'`); smoke-test (one representative call) whatever didn't change.
   For every mutating endpoint tested, confirm an audit event was recorded. Confirm rate limiting
   fires on at least one `WRITE`-tier and one `AUTH`-tier endpoint, and that `/healthz`, `/readyz`,
   `/metrics` and the docs routes never 429 regardless of request volume.
6. **Changelog.** Add a new version section to `CHANGELOG.md` (Keep a Changelog style), written
   from the actual merged history, not from the original PR descriptions (titles drift from what
   shipped).
7. **Branch → PR → merge.** Create a release branch, commit only the files the release actually
   touches (never editor/agent scratch directories), push, open a PR filled out against
   `.github/PULL_REQUEST_TEMPLATE.md`, wait for CI to go green (fixing forward on the same branch
   if it doesn't), get the diff confirmed, merge, then delete the branch.
8. **Tag and publish.** Tag `vX.Y.Z` from the merged `main`, push the tag, and create the GitHub
   release from the CHANGELOG section.

Steps 7 and 8 contain irreversible or externally-visible actions (pushing, merging, tagging,
publishing a release, deleting a branch) — an agent running this checklist confirms with the
human operator before each one rather than running the whole sequence unattended.

## Consequences
- Positive: a release agent — human or AI — has a single checklist to follow instead of
  re-deriving the process, and the known failure modes (stale pinned `APP_VERSION` in `.env`,
  comments re-accumulating ADR references, QA accidentally run against the dev hot-reload stack
  instead of production mode) are written down once instead of being rediscovered each time.
- Positive: full-depth QA is scoped to what changed since the previous tag rather than
  re-testing the entire surface from scratch every release, while still guaranteeing every
  endpoint in the live `/openapi.json` gets at least a smoke check.
- Negative: this is a process document, not an enforced one — nothing in CI fails a release
  that skips a step here. It relies on whoever cuts the release (or the plan driving an AI
  agent through it) actually following the checklist.
- Not covered: rollback procedure for a bad release. That is a deployment-environment concern
  outside this repository's scope and should get its own ADR if/when a specific deployment
  target is adopted.
