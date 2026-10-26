# Contributing to Fastkern

Thank you for your interest in contributing to Fastkern!

Contributions are welcome, including bug fixes, improvements, documentation, tests, architecture improvements, and new capabilities.

This guide explains the expected development workflow and quality standards for contributions.

## Setting Up Your Development Environment

### Cloning the Repository

Fork the repository and clone your fork:

```bash
git clone <your-fork-url>
cd <repository-directory>
```

Create a dedicated branch for your work:

```bash
git checkout -b feature/your-feature
```

Use a branch name that clearly describes the purpose of the change. For example:

```text
feature/order-module
fix/session-revocation
docs/contributing-guide
refactor/notification-adapter
```

### Installing Dependencies

This project uses `uv` for Python dependency management.

Install the project dependencies:

```bash
make install
```

Alternatively:

```bash
uv sync
```

You can run commands through `uv` without manually activating the virtual environment:

```bash
uv run python your_script.py
```

### Starting the Development Environment

For a complete Docker-based environment:

```bash
cp .env.example .env
docker compose up -d --build
docker compose --profile tools run --rm seed
```

For running the API directly on the host:

```bash
make docker-up
make migrate
make seed
make dev
```

## Making Contributions

### Follow the Architecture

This project follows a modular monolith and Clean Architecture.

New feature modules should follow:

```text
src/app/modules/<module>/
├── domain/
├── application/
├── infrastructure/
└── presentation/
```

Keep dependencies pointing inward:

```text
presentation → infrastructure → application → domain
```

Do not introduce framework or infrastructure dependencies into the domain layer.

For module-specific guidance, see [`docs/module-guide.md`](./docs/module-guide.md).

### Coding Standards

Contributions should:

- Follow the existing project structure and conventions.
- Keep business logic independent from framework concerns.
- Use type hints consistently.
- Include tests for new behavior and bug fixes.
- Avoid unnecessary cross-module coupling.
- Update documentation when behavior or architecture changes.
- Add an ADR when introducing a significant architectural decision.

### Comments and Docstrings

Comments are read by people who weren't there when the code was written, so they explain what
the code cannot say on its own:

- Explain *why*: the constraint, the trap, the reason for a non-obvious choice. Don't restate
  what the code does.
- Explain one thing in one place. Elsewhere, point to it in one line instead of repeating it.
- Describe the code as it is. History ("used to", "before this fix") belongs in commit messages.
- Don't reference ADRs, `CLAUDE.md`, or internal plans from code: state the reason itself.
  The `no-adr-references` and `no-claude-md-references` pre-commit hooks enforce this.
- Start a docstring with what the function returns or does; add detail only for a real pitfall.
- Wrap at 100 columns, like the code.

### Run Formatting and Linting

Before submitting changes:

```bash
make format
make lint
make typecheck
make arch-check
```

These commands cover:

- Ruff formatting and linting
- Strict mypy type checking
- Clean Architecture boundary validation

### Run Tests

Run the standard test suite:

```bash
make test
```

For changes that affect PostgreSQL-specific behavior, also validate against PostgreSQL.

For application-level changes, run the relevant end-to-end tests:

```bash
make e2e
```

If the change affects rate limiting:

```bash
make e2e-ratelimit
```

Only run destructive tests against a development/test environment:

```bash
make e2e-destructive
```

### Run Pre-commit

Install the hooks:

```bash
pre-commit install
```

Run all repository checks manually:

```bash
make precommit
```

The pre-commit configuration checks formatting, linting, type safety, architecture boundaries, secrets, and repository hygiene.

Fix all failures before opening a pull request.

## Commit and Branch Guidelines

Use focused commits that describe the change clearly.

Examples:

```text
feat(auth): add session revocation
fix(tenant): enforce tenant isolation
docs: update module development guide
test(rate-limit): cover proxy IP handling
refactor(notification): separate delivery adapter
```

Avoid mixing unrelated changes in the same commit or pull request.

## Pull Requests

Before opening a pull request:

1. Make sure the branch contains only related changes.
2. Run formatting and linting.
3. Run type checking.
4. Run architecture checks.
5. Run the relevant test suites.
6. Update documentation when necessary.
7. Add or update an ADR for significant architectural changes.
8. Review the final diff before pushing.

Push your branch:

```bash
git push origin feature/your-feature
```

Then open a pull request against the project's main branch.

### Pull Request Expectations

A pull request should provide:

- A clear description of the problem.
- A concise explanation of the solution.
- Relevant tests or validation steps.
- Documentation updates when applicable.
- Any known limitations or follow-up work.

Keep pull requests focused and reasonably sized so they can be reviewed effectively.

## Code Review

All contributions are subject to code review.

Reviewers may request changes related to:

- Correctness
- Security
- Architecture
- Test coverage
- Maintainability
- Performance
- Documentation
- Consistency with project conventions

Contributors are expected to address review feedback or explain why a proposed change should be handled differently.

Once the required reviews and CI checks have passed, the pull request can be merged according to the repository's branch protection and merge policy.

## Keeping Your Fork Updated

Before starting new work, synchronize your fork with the upstream repository:

```bash
git fetch upstream
git checkout main
git merge upstream/main
```

Then create a fresh feature branch.

Keeping branches current helps reduce merge conflicts and makes reviews easier.

## Documentation Contributions

Documentation improvements are welcome.

When changing behavior, architecture, configuration, development workflows, or public APIs, update the relevant documentation in the same pull request.

Useful documentation locations include:

```text
README.md          # overview and getting started only
docs/*.md          # guides: architecture, development, testing, security, production, ...
docs/module-guide.md
docs/adr/          # architecture decisions
mkdocs.yml         # navigation of the documentation site built from docs/
CLAUDE.md          # rules for AI coding agents
```

Everything in `docs/` is also published as the documentation site. Add a new guide to `nav` in
`mkdocs.yml`, and preview it with `make docs`; `make docs-build` fails on a broken link the way
CI does.

## Security Issues

Do not disclose security vulnerabilities through public issues.

Use the project's private security reporting process if one is configured for the repository. If no private reporting channel has been configured yet, contact the project maintainers before publicly disclosing a vulnerability.

## Questions and Discussions

For questions, proposals, or architectural discussions, use the repository's configured GitHub Issues or Discussions.

When proposing a substantial architectural change, include the motivation, alternatives considered, trade-offs, and expected impact.

Thank you for contributing to Fastkern!
