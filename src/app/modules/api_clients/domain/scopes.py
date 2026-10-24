"""The catalog of every scope a partner API key can be given.

Deliberately separate from the RBAC permission catalog in `roles.domain.permissions`
(scopes are independent of RBAC, so editing a tenant's roles never changes what a partner
key can do) and code-defined for the same reason that one is: a scope only means something
because a partner route checks it with `require_scope(...)`.
`tests/unit/test_permission_catalog.py` fails `make test` if a checked scope is missing
here, or an entry here is checked nowhere.

No `*` entry: a partner key gets a narrow, explicit grant, never "everything".
`ApiKey.has_scope` still honors a `*` stored before this catalog existed.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ApiKeyScopeDefinition:
    code: str

    @property
    def resource(self) -> str:
        return self.code.partition(":")[0]

    @property
    def action(self) -> str:
        return self.code.partition(":")[2]


API_KEY_SCOPE_CATALOG: tuple[ApiKeyScopeDefinition, ...] = (
    ApiKeyScopeDefinition("posts:read"),
    ApiKeyScopeDefinition("users:read"),
    ApiKeyScopeDefinition("roles:read"),
    ApiKeyScopeDefinition("master_examples:read"),
    ApiKeyScopeDefinition("examples:read"),
    ApiKeyScopeDefinition("files:read"),
)

# `resource:action`, lowercase snake_case segments — checked before the catalog lookup so an
# arbitrary client string never ends up echoed back in an `UnknownScopeError`.
SCOPE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*:[a-z][a-z0-9_]*$")
MAX_SCOPES_PER_KEY = 100

_KNOWN_CODES = frozenset(s.code for s in API_KEY_SCOPE_CATALOG)


def is_valid_scope(scope: str) -> bool:
    return SCOPE_PATTERN.fullmatch(scope) is not None


def unknown_scopes(scopes: Iterable[str]) -> list[str]:
    return sorted(set(scopes) - _KNOWN_CODES)
