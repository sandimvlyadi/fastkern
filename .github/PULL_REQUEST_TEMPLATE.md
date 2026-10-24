## Summary

<!--
Describe what this pull request changes and why.
Keep this section focused on the problem and the solution.
-->

## Related Issue

<!--
Link the related issue, for example:
Closes #123
Fixes #123
Related to #123
-->

## Type of Change

- [ ] Bug fix
- [ ] New feature
- [ ] Enhancement
- [ ] Refactoring
- [ ] Performance improvement
- [ ] Security improvement
- [ ] Documentation
- [ ] Tests
- [ ] CI/CD or developer tooling
- [ ] Database / migration
- [ ] Other

## Changes

<!--
List the important changes introduced by this PR.
-->

-
-
-

## Architecture / Design

<!--
For architectural or cross-module changes, explain:
- Which module/layer was changed
- Why the change belongs there
- Any new dependency or integration
- Whether an ADR is required
-->

## Testing

<!--
Describe how the changes were tested.
Include relevant commands and important test scenarios.
-->

### Automated Checks

- [ ] `make lint`
- [ ] `make format`
- [ ] `make typecheck`
- [ ] `make arch-check`
- [ ] `make test`
- [ ] `make precommit`

### End-to-End Tests

- [ ] Not applicable
- [ ] `make e2e`
- [ ] `make e2e-ratelimit`
- [ ] `make e2e-destructive`

## Database Changes

- [ ] No database changes
- [ ] New migration included
- [ ] Existing migration modified before first deployment
- [ ] Migration has been tested against PostgreSQL

<!--
If there are database changes, explain migration/deployment considerations here.
-->

## Security Considerations

- [ ] No security-sensitive changes
- [ ] Authentication / authorization affected
- [ ] Tenant isolation affected
- [ ] Session / token handling affected
- [ ] Rate limiting affected
- [ ] Secrets / configuration affected
- [ ] Security headers / CORS affected
- [ ] Other security impact

<!--
Explain security implications when applicable.
Never include secrets, credentials, tokens, private keys, or sensitive production data.
-->

## Production Readiness

- [ ] No production configuration changes
- [ ] Configuration/environment variables changed
- [ ] Deployment procedure changed
- [ ] Observability/metrics/logging changed
- [ ] Backward compatibility considered
- [ ] Rollback strategy considered

## Documentation

- [ ] No documentation changes required
- [ ] README updated
- [ ] Module documentation updated
- [ ] ADR added/updated
- [ ] API documentation updated
- [ ] Other documentation updated

## Checklist

- [ ] I have reviewed my own changes and the final diff.
- [ ] I have added or updated tests where appropriate.
- [ ] I have not introduced unnecessary cross-module coupling.
- [ ] I have followed the project's Clean Architecture boundaries.
- [ ] I have not committed secrets, credentials, tokens, or private information.
- [ ] I have updated documentation where necessary.
- [ ] I have added an ADR for significant architectural decisions, where applicable.
- [ ] This PR contains only related changes.
