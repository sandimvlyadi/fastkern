"""Use cases for the `api_clients` module — one class per use case."""

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.core.pagination import OffsetPage, OffsetParams, paginate_sequence
from app.modules.api_clients.application.dto import ApiKeyScopeEntry, CreateApiKeyInput
from app.modules.api_clients.domain.entities import ApiKey
from app.modules.api_clients.domain.exceptions import (
    ApiClientNotFoundError,
    ApiKeyAlreadyExpiredError,
    ApiKeyAlreadyRevokedError,
    ApiKeyExpiredError,
    InvalidApiKeyError,
    ScopeEscalationError,
    UnknownScopeError,
)
from app.modules.api_clients.domain.hashing import generate_api_key, hash_api_key
from app.modules.api_clients.domain.ports import ApiKeyRepositoryPort
from app.modules.api_clients.domain.scopes import API_KEY_SCOPE_CATALOG, unknown_scopes
from app.modules.roles.domain.entities import missing_grants
from app.modules.roles.domain.ports import UserRoleRepositoryPort


async def _ensure_can_grant_scopes(
    user_roles: UserRoleRepositoryPort, caller_id: UUID, scopes: Iterable[str]
) -> None:
    """A caller may only hand a key the scopes they hold as RBAC permissions of the same name
    (across all their assigned roles, the rule role editing already follows). Without this,
    `api_clients:write` alone is a path to reading every user and file in the tenant: mint (or
    rotate) a key carrying `users:read`/`files:read` and use it yourself."""
    missing = missing_grants(await user_roles.list_roles_for_user(caller_id), scopes)
    if missing:
        raise ScopeEscalationError(scopes=", ".join(missing))


class CreateApiKeyUseCase:
    def __init__(self, api_keys: ApiKeyRepositoryPort, user_roles: UserRoleRepositoryPort) -> None:
        self._api_keys = api_keys
        self._user_roles = user_roles

    async def execute(self, data: CreateApiKeyInput, caller_id: UUID) -> tuple[ApiKey, str]:
        unknown = unknown_scopes(data.scopes)
        if unknown:
            raise UnknownScopeError(scopes=", ".join(unknown))
        await _ensure_can_grant_scopes(self._user_roles, caller_id, data.scopes)
        plaintext, key_prefix, key_hash = generate_api_key()
        api_key = ApiKey(
            tenant_id=data.tenant_id,
            name=data.name,
            key_prefix=key_prefix,
            key_hash=key_hash,
            scopes=list(data.scopes),
            expires_at=data.expires_at,
        )
        created = await self._api_keys.add(api_key)
        return created, plaintext


class RotateApiKeyUseCase:
    def __init__(self, api_keys: ApiKeyRepositoryPort, user_roles: UserRoleRepositoryPort) -> None:
        self._api_keys = api_keys
        self._user_roles = user_roles

    async def execute(
        self, api_key_id: UUID, *, caller_id: UUID, overlap_hours: int
    ) -> tuple[ApiKey, str]:
        api_key = await self._api_keys.get_by_id(api_key_id)
        if api_key is None:
            raise ApiClientNotFoundError()
        now = datetime.now(UTC)
        # Either way the new plaintext could never pass VerifyApiKeyUseCase, so refuse with a
        # clear reason instead of handing out a dead key.
        if api_key.status != "active":
            raise ApiKeyAlreadyRevokedError()
        if api_key.is_expired(now=now):
            raise ApiKeyAlreadyExpiredError()
        # Rotating hands the caller a working plaintext for every scope on the key, so it needs
        # the same grant check as creating one.
        await _ensure_can_grant_scopes(self._user_roles, caller_id, api_key.scopes)
        plaintext, key_prefix, key_hash = generate_api_key()
        api_key.previous_key_hash = api_key.key_hash
        api_key.previous_key_expires_at = now + timedelta(hours=overlap_hours)
        api_key.key_prefix = key_prefix
        api_key.key_hash = key_hash
        saved = await self._api_keys.save(api_key)
        return saved, plaintext


class RevokeApiKeyUseCase:
    def __init__(self, api_keys: ApiKeyRepositoryPort) -> None:
        self._api_keys = api_keys

    async def execute(self, api_key_id: UUID) -> ApiKey:
        api_key = await self._api_keys.get_by_id(api_key_id)
        if api_key is None:
            raise ApiClientNotFoundError()
        api_key.status = "revoked"
        # Clear both hashes so a revoked key can never match get_by_key_hash() again,
        # even during what would otherwise still be a rotation overlap window.
        api_key.key_hash = ""
        api_key.previous_key_hash = None
        api_key.previous_key_expires_at = None
        return await self._api_keys.save(api_key)


class ListApiKeysUseCase:
    def __init__(self, api_keys: ApiKeyRepositoryPort) -> None:
        self._api_keys = api_keys

    async def execute(self, params: OffsetParams) -> OffsetPage[ApiKey]:
        return await self._api_keys.paginate(params)


class ListApiKeyScopesUseCase:
    """The scopes an API key can be given (`API_KEY_SCOPE_CATALOG`), each flagged with whether
    `caller_id` may grant it (the rule `_ensure_can_grant_scopes` enforces), so a client can
    grey out what a create would refuse."""

    def __init__(self, user_roles: UserRoleRepositoryPort) -> None:
        self._user_roles = user_roles

    async def execute(self, caller_id: UUID, params: OffsetParams) -> OffsetPage[ApiKeyScopeEntry]:
        held = await self._user_roles.list_roles_for_user(caller_id)
        entries = [
            ApiKeyScopeEntry(
                code=scope.code,
                resource=scope.resource,
                action=scope.action,
                grantable=not missing_grants(held, [scope.code]),
            )
            for scope in API_KEY_SCOPE_CATALOG
        ]
        return paginate_sequence(entries, params)


class VerifyApiKeyUseCase:
    """Runs on every partner request, so it is one indexed `get_by_key_hash` lookup and no
    write. Saving `last_used_at` here would lock the key's row until the request's transaction
    commits, queueing every concurrent request made with the same key behind it; the
    request-log task records it off the request path instead."""

    def __init__(self, api_keys: ApiKeyRepositoryPort) -> None:
        self._api_keys = api_keys

    async def execute(self, plaintext: str) -> ApiKey:
        key_hash = hash_api_key(plaintext)
        api_key = await self._api_keys.get_by_key_hash(key_hash)
        # Revoking clears both hashes, so a revoked key normally never matches; the status
        # check covers a row whose status was changed some other way.
        if api_key is None or api_key.status != "active":
            raise InvalidApiKeyError()
        now = datetime.now(UTC)
        if api_key.is_expired(now=now):
            raise ApiKeyExpiredError()
        if api_key.key_hash != key_hash and not api_key.previous_key_usable(now=now):
            raise InvalidApiKeyError()
        return api_key
