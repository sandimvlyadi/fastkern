# Security Policy

## Overview

Fastkern is intended to provide a production-oriented foundation for backend services. Security issues should be reported responsibly so they can be investigated and addressed before details become publicly available.

If you believe you have found a security vulnerability, please do **not** disclose it publicly through a GitHub issue, pull request, discussion, or other public channel.

## Reporting a Vulnerability

Please report security vulnerabilities privately to:

**Sandi Mulyadi**
**Email:** sandimvlyadi@gmail.com

When reporting a vulnerability, please provide enough information for the issue to be reproduced and investigated.

A useful report should include, where applicable:

- A clear description of the vulnerability.
- The affected component, endpoint, module, or configuration.
- Steps to reproduce the issue.
- The potential security impact.
- Any required authentication, permissions, or environment conditions.
- A proof of concept, if available and safe to provide.
- Relevant logs, requests, responses, or screenshots with sensitive information removed.
- A suggested mitigation or fix, if you have one.

Please avoid including real credentials, private keys, tokens, personal data, or other secrets in the report.

## Coordinated Disclosure

Please allow reasonable time for the vulnerability to be investigated and addressed before publicly disclosing technical details.

Security reports will be handled confidentially to the extent reasonably possible.

After a fix is available, the project may publish a security advisory or other notification describing the issue at an appropriate level of detail.

## Security-Sensitive Changes

Contributors working on security-sensitive areas should take extra care with:

- Authentication and session management
- Authorization and RBAC
- Multi-tenant isolation
- JWT handling
- Password and credential handling
- Session revocation
- Rate limiting
- CORS configuration
- Security headers
- Production configuration guards
- Database access and migrations
- External API integrations
- Background jobs
- Logging and telemetry
- Secrets and environment variables

Changes affecting these areas should include appropriate tests and documentation.

For significant architectural security decisions, document the decision in an Architecture Decision Record under:

```text
docs/adr/
```

## Secrets

Never commit secrets to the repository.

This includes:

- API keys
- Passwords
- JWT secrets
- Private keys
- Access tokens
- Database credentials
- SMTP credentials
- Production environment files

Use environment variables and the project's configuration system instead.

The repository uses secret scanning through Gitleaks as part of the development checks.

If a secret is accidentally committed:

1. Treat it as compromised immediately.
2. Rotate or revoke the secret.
3. Remove it from the repository history when appropriate.
4. Report the incident privately if it affects the project's infrastructure or users.

Removing a secret from the latest commit alone does not make a previously exposed secret safe.

## Production Configuration

Before deploying the application, follow the production requirements in [`docs/production.md`](./docs/production.md).

In particular:

- Never use placeholder secrets in production.
- Keep `DEBUG=false`.
- Configure real CORS origins.
- Protect `/metrics` from public access.
- Configure trusted proxy addresses correctly.
- Use a production email backend.
- Use a strong `SECRET_KEY`.
- Do not expose development credentials or seed passwords.
- Run database migrations through the deployment process.

## Dependency Security

Keep dependencies up to date and review security advisories affecting the project.

When upgrading dependencies:

1. Review the changelog and breaking changes.
2. Run the complete test suite.
3. Run linting and type checks.
4. Run architecture checks.
5. Review dependency changes before merging.
6. Run the relevant end-to-end tests for infrastructure-sensitive changes.

Security updates should be prioritized when they address vulnerabilities affecting a deployed component.

## Security Testing

Security-sensitive changes should include regression tests where practical.

The project provides several validation layers:

```bash
make lint
make typecheck
make arch-check
make test
make precommit
```

For application-level security changes, also run the relevant end-to-end tests:

```bash
make e2e
```

For rate-limiting changes:

```bash
make e2e-ratelimit
```

## Supported Versions

This project is currently maintained as a boilerplate/template repository.

Security fixes should generally be applied to the current development version. If the project later publishes supported release versions, this section should be updated with an explicit support matrix.

Example:

| Version | Supported |
|---|---|
| Current development | Yes |
| Older releases | See release policy |

## Security Contact

**Sandi Mulyadi**
**Email:** sandimvlyadi@gmail.com

Please use this address for private security vulnerability reports.

Thank you for helping keep Fastkern and its users secure.
