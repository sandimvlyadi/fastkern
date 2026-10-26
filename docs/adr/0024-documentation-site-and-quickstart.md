# 24. Documentation site and a quickstart from the published image

## Status

Accepted

## Context

The guides in `docs/` were only readable on GitHub: no search, no navigation across pages, and
nothing a search engine finds as the project's home. Since ADR 0022 every release is also a public
image on GHCR, but the only documented way to start fastkern was still a checkout and a local
build. Someone who only wanted to look at the API first had to clone the repository and build the
image themselves.

Fastkern is a boilerplate, so the image is not the product: what people take away is the code.
Trying the image and building a service on the code are two different readers, and the docs
addressed only the second.

The obvious site generator for a Python project with Markdown docs, Material for MkDocs, entered
maintenance mode in November 2025: no new features after 9.7, and fixes only until its end of life
in 2027. MkDocs 1.x itself has had no release since August 2024, and the planned MkDocs 2.0
drops the plugin and theme system Material depends on.

## Decision

- **Two entry points**, in the README and on the site's home page: *Try it from the image*
  (`docs/quickstart.md`) and *Build your service* (`docs/new-service.md`). The repository is a
  GitHub template repository, so *Build your service* starts from **Use this template**.
- **`docker-compose.quickstart.yml`**, standalone like `docker-compose.prod.yml`: it runs
  `ghcr.io/sandimvlyadi/fastkern:latest` (overridable with `FASTKERN_IMAGE`) with PostgreSQL,
  Redis and Mailpit, needs no `.env`, and runs with `ENVIRONMENT=local`, so the app's production
  guards keep it off a server. It uses `latest`, which ADR 0022 keeps for exactly this. CI boots
  it on the image built for each pull request, seeds it and logs in, so it keeps working with
  the next release.
- **The site is built with [Zensical](https://zensical.org)** from the unchanged Markdown in
  `docs/` and `mkdocs.yml`, and published to GitHub Pages from `main` by
  `.github/workflows/docs.yml`. Pull requests that touch the docs run a strict build that fails on
  a broken link. Zensical is the successor to Material for MkDocs from the same team, reads
  `mkdocs.yml`, and installs with uv like the rest of the toolchain. It is pinned through
  `uv.lock` in its own `docs` dependency group.
- **Links from `docs/` to files outside it are full GitHub URLs**: the site contains only
  `docs/`, so a relative `../` link would break there.

Alternatives considered:

- *Material for MkDocs 9.7*: works today, but would need replacing within a year or two.
- *Sphinx with MyST and Furo*: mature, but its strength is API reference from docstrings, which a
  service whose API is documented by OpenAPI doesn't need, at the cost of more configuration.
- *VitePress or Starlight*: good sites, but they add a Node toolchain and npm dependencies to a
  Python repository. VitePress is the fallback if Zensical doesn't work out, since it can also
  build from `docs/` in place.
- *mdBook, Jekyll themes*: plainer output, and Jekyll themes want front matter in every page,
  which shows up as a table when the page is read on GitHub.

## Consequences

- Positive: a first look at fastkern takes one compose file and no toolchain, and is tested on
  every pull request.
- Positive: the docs get search and navigation without changing how they are written or read on
  GitHub.
- Negative: Zensical is young (0.0.x when adopted, with a stable 0.1 line announced). A breaking
  release can fail the docs build; it cannot affect the application or its CI, since the docs
  build is a separate, path-filtered workflow.
- Negative: on `main`, the quickstart compose file can be newer than the latest release for as
  long as an unreleased change needs it. `FASTKERN_IMAGE` pins an older release when that matters.
- A repository created from the template inherits the docs workflow, but its deploy job runs only
  in this repository until the new owner changes the repository name in it (`docs/new-service.md`
  lists this).
