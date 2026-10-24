# 1. Record architecture decisions

## Status
Accepted

## Context
This project is meant to be a long-lived base for many future services.
Without a record of *why* a decision was made, future maintainers (human
or AI) will either violate the original intent by accident or waste time
re-litigating settled trade-offs.

## Decision
We use lightweight Architecture Decision Records (ADRs), one per
significant decision, numbered sequentially in `docs/adr/`. Each ADR has:
Status, Context, Decision, Consequences.

## Consequences
Any change that touches a documented decision must either comply with it
or add a new ADR that supersedes it. `CLAUDE.md` requires agents to check
this directory before proposing an architectural change.
