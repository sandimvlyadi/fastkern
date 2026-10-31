# syntax=docker/dockerfile:1
# Both stages use the same Python, pinned by digest so a rebuild is reproducible; Dependabot
# (.github/dependabot.yml) proposes the new digest when the upstream image gets security fixes.
FROM python:3.15-slim@sha256:94d3426f1def7db4a106a232ac121bfd975393e5dfd40a619b955fae45fe7913 AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# uv from Astral's image, pinned by version and digest (the same version CI uses), instead of
# piping an unpinned install script into a shell. Bump both together.
COPY --from=ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff /uv /uvx /bin/

# Same path as the runtime stage: a venv's console scripts hard-code the
# interpreter path in their shebang, so building elsewhere breaks them.
WORKDIR /app
COPY pyproject.toml uv.lock* README.md ./
RUN uv sync --no-install-project --no-dev

COPY src ./src
RUN uv sync --no-dev

# ---------------------------------------------------------------------------
FROM python:3.15-slim@sha256:94d3426f1def7db4a106a232ac121bfd975393e5dfd40a619b955fae45fe7913 AS runtime

# The app runs from /app/.venv, so the base image's own pip is dead weight -- and its vendored
# urllib3/msgpack show up in image CVE scans. Remove it rather than carry it.
RUN python -m pip uninstall --yes --root-user-action=ignore pip \
    && groupadd --system app && useradd --system --gid app --home /app app

WORKDIR /app
COPY --from=base /app/.venv /app/.venv
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations
# Ops scripts (e.g. `python scripts/seed.py`, run via the compose `tools` profile).
COPY scripts ./scripts

# Writable storage root for uploads (STORAGE_ROOT defaults to `data`, i.e. /app/data). The named
# volume in docker-compose.yml copies this ownership on first use.
RUN mkdir -p /app/data && chown app:app /app/data && chmod 750 /app/data

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONPATH="/app/src" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER app
EXPOSE 8000

# This HEALTHCHECK only fits the API process (it probes /healthz). Anything else built from this
# image that does not serve HTTP (the Celery worker) MUST override it -- see docker-compose.yml.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
