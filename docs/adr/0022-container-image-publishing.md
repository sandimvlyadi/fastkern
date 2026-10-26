# 22. Container image publishing to GitHub Container Registry

## Status

Accepted

## Context

Until now the only way to run fastkern was to build the image from a checkout
(`docker compose up --build`). That works for development, but a deployment then depends on
whatever the build host produces on the day: a rebuild of the same tag can pick up different
Debian packages, and a rollback ([Rolling back a release](../rollback.md)) has to check out and
rebuild the previous tag instead of redeploying a known artifact. Developers on Apple silicon and
the x86 servers it is deployed to also need the same image for different CPU architectures.

ADR 0018's last step tags a release and publishes a GitHub release, but produces no runnable
artifact.

## Decision

- **Registry:** `ghcr.io/sandimvlyadi/fastkern`, public, pushed by the repository's own workflow
  with `GITHUB_TOKEN`. No extra secret, and the package is linked to the repository through the
  `org.opencontainers.image.source` label.
- **Trigger:** `.github/workflows/publish-image.yml` runs on a pushed `vX.Y.Z` tag. A manual
  `workflow_dispatch` with a `tag` input publishes a tag that already exists (one cut before the
  workflow existed, or a failed run to repeat), with an opt-out for moving `latest` when
  republishing an older release. A pull request touching the workflow or the `Dockerfile` runs
  the same build without pushing.
- **Platforms:** `linux/amd64` and `linux/arm64` in one multi-platform image, built with QEMU.
- **Tags:** `X.Y.Z`, `X.Y` and `latest`. Deployments pin `X.Y.Z` or, better, the digest the
  workflow prints in its summary. `latest` exists only for trying the image out; nothing in this
  repository's deployment docs uses it.
- **Gates before anything is pushed:** the tag must match `pyproject.toml`'s version, the amd64
  image must pass the same Trivy scan as CI (fixable HIGH/CRITICAL CVEs), and a smoke test must
  import the app and start `alembic`, `celery` and `uvicorn`'s CLIs. After the push, the smoke
  test runs again on every platform, by digest.
- **Release process:** this extends ADR 0018's step 8. After the GitHub release, confirm the
  workflow published the image, then pull it by tag on a machine of the other architecture and
  boot the stack from it.

Per-commit images from `main` (`sha-<commit>`) and the clean-up of old versions they would need
are left out until a deployment target actually consumes them.

## Consequences

- Positive: a release is a fixed, scanned artifact for both architectures. Rollback redeploys the
  previous tag's image instead of rebuilding it.
- Positive: the workflow cannot publish a tag whose code reports another version, or an image
  that fails to start its process types.
- Negative: the arm64 half is emulated, so a publish run takes several times longer than CI's
  amd64-only build. If that becomes a problem, native arm64 runners with a manifest merge step
  replace QEMU without changing the published image.
- Negative: only the amd64 image is scanned before push. Both are built from the same digest-pinned
  Debian base, so the package set is the same, but an arm64-only CVE would not fail the run.
- The first publish creates the package as private; making it public is a one-time change in the
  package's settings on GitHub, not something the workflow can do.
