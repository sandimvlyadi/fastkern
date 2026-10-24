"""In-memory fakes implementing the domain ports, shared by unit tests that
exercise use cases without touching a real database or Redis."""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import Any
from uuid import UUID

from app.core.context import get_request_context
from app.core.pagination import OffsetPage, OffsetParams
from app.modules.api_clients.domain.entities import ApiKey
from app.modules.api_clients.domain.ports import ApiKeyRepositoryPort
from app.modules.audit_log.domain.entities import AuditLogEntry
from app.modules.audit_log.domain.ports import AuditLogRepositoryPort
from app.modules.auth.domain.ports import TokenSessionStorePort
from app.modules.examples.domain.entities import Example
from app.modules.examples.domain.ports import ExampleRepositoryPort
from app.modules.files.domain.entities import FileAsset, FileVariant, UploadChunk, UploadSession
from app.modules.files.domain.ports import (
    ChunkAlreadyReceivedError,
    ChunkRepositoryPort,
    FileRepositoryPort,
    ObjectStoragePort,
    UploadRepositoryPort,
)
from app.modules.files.domain.state import FileStatus, UploadStatus
from app.modules.files.domain.value_objects import StorageKey
from app.modules.master_examples.domain.entities import MasterExample
from app.modules.master_examples.domain.ports import MasterExampleRepositoryPort
from app.modules.notifications.domain.ports import NotificationSenderPort
from app.modules.posts.domain.entities import Post
from app.modules.posts.domain.ports import PostsApiPort
from app.modules.roles.domain.entities import Role, UserRoleAssignment
from app.modules.roles.domain.ports import RoleRepositoryPort, UserRoleRepositoryPort
from app.modules.tenants.domain.entities import Tenant
from app.modules.tenants.domain.ports import TenantRepositoryPort, TenantRoleProvisionerPort
from app.modules.users.domain.entities import User
from app.modules.users.domain.ports import UserRepositoryPort


class InMemoryApiKeyRepository(ApiKeyRepositoryPort):
    def __init__(self) -> None:
        self.keys: dict[UUID, ApiKey] = {}

    async def get_by_id(self, api_key_id: UUID, *, tenant_scoped: bool = True) -> ApiKey | None:
        key = self.keys.get(api_key_id)
        if key is None:
            return None
        if tenant_scoped:
            ambient_tenant_id = get_request_context().tenant_id
            if ambient_tenant_id is not None and key.tenant_id != ambient_tenant_id:
                return None
        return key

    async def get_by_key_hash(self, key_hash: str) -> ApiKey | None:
        for key in self.keys.values():
            if key.key_hash == key_hash or key.previous_key_hash == key_hash:
                return key
        return None

    async def add(self, api_key: ApiKey) -> ApiKey:
        self.keys[api_key.id] = api_key
        return api_key

    async def save(self, api_key: ApiKey) -> ApiKey:
        self.keys[api_key.id] = api_key
        return api_key

    async def paginate(self, params: OffsetParams) -> OffsetPage[ApiKey]:
        items = list(self.keys.values())
        start = (params.page - 1) * params.page_size
        page_items = items[start : start + params.page_size]
        return OffsetPage(
            items=page_items, total=len(items), page=params.page, page_size=params.page_size
        )


class InMemoryUserRepository(UserRepositoryPort):
    def __init__(self) -> None:
        self.users: dict[UUID, User] = {}
        self.deleted_ids: list[UUID] = []

    async def get_by_id(self, user_id: UUID, *, tenant_scoped: bool = True) -> User | None:
        user = self.users.get(user_id)
        if user is None:
            return None
        if tenant_scoped:
            # Mirrors BaseRepository._apply_scopes: filter by the ambient
            # tenant when one is set, so a test that deliberately sets
            # tenant context to exercise cross-tenant rejection gets real
            # isolation instead of false confidence. Tests that never set
            # tenant context (the common case) see unchanged behavior.
            ambient_tenant_id = get_request_context().tenant_id
            if ambient_tenant_id is not None and user.tenant_id != ambient_tenant_id:
                return None
        return user

    async def get_by_email(self, email: str, *, tenant_id: UUID) -> User | None:
        for user in self.users.values():
            if user.email == email and user.tenant_id == tenant_id:
                return user
        return None

    async def add(self, user: User) -> User:
        self.users[user.id] = user
        return user

    async def save(self, user: User) -> User:
        self.users[user.id] = user
        return user

    async def delete(self, user_id: UUID) -> None:
        self.deleted_ids.append(user_id)
        self.users.pop(user_id, None)

    async def paginate(self, params: OffsetParams) -> OffsetPage[User]:
        items = list(self.users.values())
        start = params.offset
        page_items = items[start : start + params.page_size]
        return OffsetPage(
            items=page_items, total=len(items), page=params.page, page_size=params.page_size
        )


class InMemoryRoleRepository(RoleRepositoryPort):
    def __init__(self) -> None:
        self.roles: dict[UUID, Role] = {}
        self.deleted_ids: list[UUID] = []

    async def get_by_id(self, role_id: UUID) -> Role | None:
        role = self.roles.get(role_id)
        if role is None:
            return None
        ambient_tenant_id = get_request_context().tenant_id
        if ambient_tenant_id is not None and role.tenant_id != ambient_tenant_id:
            return None
        return role

    async def get_by_name(self, name: str, *, tenant_id: UUID | None = None) -> Role | None:
        effective_tenant_id = (
            tenant_id if tenant_id is not None else get_request_context().tenant_id
        )
        for role in self.roles.values():
            if role.name == name and (
                effective_tenant_id is None or role.tenant_id == effective_tenant_id
            ):
                return role
        return None

    async def list_all(self) -> list[Role]:
        return list(self.roles.values())

    async def paginate(self, params: OffsetParams) -> OffsetPage[Role]:
        ambient_tenant_id = get_request_context().tenant_id
        items = [
            r
            for r in self.roles.values()
            if ambient_tenant_id is None or r.tenant_id == ambient_tenant_id
        ]
        start = params.offset
        page_items = items[start : start + params.page_size]
        return OffsetPage(
            items=page_items, total=len(items), page=params.page, page_size=params.page_size
        )

    async def add(self, role: Role) -> Role:
        self.roles[role.id] = role
        return role

    async def save(self, role: Role) -> Role:
        self.roles[role.id] = role
        return role

    async def delete(self, role_id: UUID) -> None:
        self.deleted_ids.append(role_id)
        self.roles.pop(role_id, None)


class InMemoryMasterExampleRepository(MasterExampleRepositoryPort):
    def __init__(self) -> None:
        self.examples: dict[UUID, MasterExample] = {}
        self.deleted_ids: list[UUID] = []

    async def get_by_id(self, example_id: UUID) -> MasterExample | None:
        example = self.examples.get(example_id)
        if example is None:
            return None
        ambient_tenant_id = get_request_context().tenant_id
        if ambient_tenant_id is not None and example.tenant_id != ambient_tenant_id:
            return None
        return example

    async def get_by_name(
        self, name: str, *, tenant_id: UUID | None = None
    ) -> MasterExample | None:
        effective_tenant_id = (
            tenant_id if tenant_id is not None else get_request_context().tenant_id
        )
        for example in self.examples.values():
            if example.name == name and (
                effective_tenant_id is None or example.tenant_id == effective_tenant_id
            ):
                return example
        return None

    async def paginate(self, params: OffsetParams) -> OffsetPage[MasterExample]:
        ambient_tenant_id = get_request_context().tenant_id
        items = [
            e
            for e in self.examples.values()
            if ambient_tenant_id is None or e.tenant_id == ambient_tenant_id
        ]
        start = params.offset
        page_items = items[start : start + params.page_size]
        return OffsetPage(
            items=page_items, total=len(items), page=params.page, page_size=params.page_size
        )

    async def add(self, example: MasterExample) -> MasterExample:
        self.examples[example.id] = example
        return example

    async def save(self, example: MasterExample) -> MasterExample:
        self.examples[example.id] = example
        return example

    async def delete(self, example_id: UUID) -> None:
        self.deleted_ids.append(example_id)
        self.examples.pop(example_id, None)


class InMemoryExampleRepository(ExampleRepositoryPort):
    def __init__(self) -> None:
        self.examples: dict[UUID, Example] = {}
        self.deleted_ids: list[UUID] = []

    async def get_by_id(self, example_id: UUID) -> Example | None:
        example = self.examples.get(example_id)
        if example is None:
            return None
        ambient_tenant_id = get_request_context().tenant_id
        if ambient_tenant_id is not None and example.tenant_id != ambient_tenant_id:
            return None
        return example

    async def paginate(self, params: OffsetParams) -> OffsetPage[Example]:
        ambient_tenant_id = get_request_context().tenant_id
        items = [
            e
            for e in self.examples.values()
            if ambient_tenant_id is None or e.tenant_id == ambient_tenant_id
        ]
        start = params.offset
        page_items = items[start : start + params.page_size]
        return OffsetPage(
            items=page_items, total=len(items), page=params.page, page_size=params.page_size
        )

    async def add(self, example: Example) -> Example:
        self.examples[example.id] = example
        return example

    async def save(self, example: Example) -> Example:
        self.examples[example.id] = example
        return example

    async def delete(self, example_id: UUID) -> None:
        self.deleted_ids.append(example_id)
        self.examples.pop(example_id, None)


class InMemoryUserRoleRepository(UserRoleRepositoryPort):
    def __init__(self) -> None:
        self.assignments: set[tuple[UUID, UUID]] = set()
        self._roles_by_id: dict[UUID, Role] = {}
        self._defaults: dict[UUID, UUID] = {}  # user_id -> role_id

    def seed_role(self, role: Role) -> None:
        self._roles_by_id[role.id] = role

    async def assign(
        self,
        user_id: UUID,
        role_id: UUID,
        *,
        tenant_id: UUID | None = None,
        is_default: bool = False,
    ) -> bool:
        if (user_id, role_id) in self.assignments:
            return False
        self.assignments.add((user_id, role_id))
        if is_default:
            self._defaults[user_id] = role_id
        return True

    async def revoke(self, user_id: UUID, role_id: UUID, *, tenant_id: UUID | None = None) -> bool:
        if (user_id, role_id) not in self.assignments:
            return False
        self.assignments.discard((user_id, role_id))
        if self._defaults.get(user_id) == role_id:
            del self._defaults[user_id]
        return True

    async def list_roles_for_user(
        self, user_id: UUID, *, tenant_id: UUID | None = None
    ) -> list[Role]:
        effective_tenant_id = (
            tenant_id if tenant_id is not None else get_request_context().tenant_id
        )
        roles = [
            self._roles_by_id[role_id]
            for (uid, role_id) in self.assignments
            if uid == user_id
            and role_id in self._roles_by_id
            and (
                effective_tenant_id is None
                or self._roles_by_id[role_id].tenant_id == effective_tenant_id
            )
        ]
        # Deterministic order, matching UserRoleRepository's `ORDER BY name`
        # (RevokeRoleFromUserUseCase's default-promotion picks roles[0]).
        return sorted(roles, key=lambda r: r.name)

    async def paginate_roles_for_user(
        self, user_id: UUID, params: OffsetParams, *, tenant_id: UUID | None = None
    ) -> OffsetPage[UserRoleAssignment]:
        roles = await self.list_roles_for_user(user_id, tenant_id=tenant_id)
        items = [
            UserRoleAssignment(role=role, is_default=self._defaults.get(user_id) == role.id)
            for role in roles
        ]
        start = params.offset
        page_items = items[start : start + params.page_size]
        return OffsetPage(
            items=page_items, total=len(items), page=params.page, page_size=params.page_size
        )

    async def get_default_role_for_user(
        self, user_id: UUID, *, tenant_id: UUID | None = None
    ) -> Role | None:
        role_id = self._defaults.get(user_id)
        if role_id is None:
            return None
        role = self._roles_by_id.get(role_id)
        if role is None:
            return None
        effective_tenant_id = (
            tenant_id if tenant_id is not None else get_request_context().tenant_id
        )
        if effective_tenant_id is not None and role.tenant_id != effective_tenant_id:
            return None
        return role

    async def set_default_role(
        self, user_id: UUID, role_id: UUID, *, tenant_id: UUID | None = None
    ) -> bool:
        if (user_id, role_id) not in self.assignments:
            return False
        self._defaults[user_id] = role_id
        return True

    async def user_has_role(
        self, user_id: UUID, role_name: str, *, tenant_id: UUID | None = None
    ) -> bool:
        roles = await self.list_roles_for_user(user_id, tenant_id=tenant_id)
        return any(r.name == role_name for r in roles)


class InMemoryTenantRepository(TenantRepositoryPort):
    def __init__(self) -> None:
        self.tenants: dict[UUID, Tenant] = {}
        self.deleted_ids: list[UUID] = []

    async def get_by_id(self, tenant_id: UUID) -> Tenant | None:
        return self.tenants.get(tenant_id)

    async def get_by_slug(self, slug: str) -> Tenant | None:
        for tenant in self.tenants.values():
            if tenant.slug == slug:
                return tenant
        return None

    async def list_all(self) -> list[Tenant]:
        return list(self.tenants.values())

    async def paginate(self, params: OffsetParams) -> OffsetPage[Tenant]:
        items = list(self.tenants.values())
        start = params.offset
        page_items = items[start : start + params.page_size]
        return OffsetPage(
            items=page_items, total=len(items), page=params.page, page_size=params.page_size
        )

    async def add(self, tenant: Tenant) -> Tenant:
        self.tenants[tenant.id] = tenant
        return tenant

    async def save(self, tenant: Tenant) -> Tenant:
        self.tenants[tenant.id] = tenant
        return tenant

    async def delete(self, tenant_id: UUID) -> None:
        self.deleted_ids.append(tenant_id)
        self.tenants.pop(tenant_id, None)


class RecordingTenantRoleProvisioner(TenantRoleProvisionerPort):
    def __init__(self) -> None:
        self.provisioned_for: list[UUID] = []

    async def provision_baseline_roles(self, tenant_id: UUID) -> None:
        self.provisioned_for.append(tenant_id)


class InMemoryAuditLogRepository(AuditLogRepositoryPort):
    def __init__(self) -> None:
        self.records: list[AuditLogEntry] = []
        self.last_paginate_kwargs: dict[str, Any] = {}

    async def record(self, entry: AuditLogEntry) -> AuditLogEntry:
        self.records.append(entry)
        return entry

    async def paginate(
        self,
        params: OffsetParams,
        *,
        resource_type: str | None = None,
        actor_id: UUID | None = None,
    ) -> OffsetPage[AuditLogEntry]:
        self.last_paginate_kwargs = {"resource_type": resource_type, "actor_id": actor_id}
        items = self.records
        if resource_type:
            items = [e for e in items if e.resource_type == resource_type]
        if actor_id:
            items = [e for e in items if e.actor_id == actor_id]
        start = params.offset
        page_items = items[start : start + params.page_size]
        return OffsetPage(
            items=page_items, total=len(items), page=params.page, page_size=params.page_size
        )


class InMemoryTokenSessionStore(TokenSessionStorePort):
    def __init__(self) -> None:
        self.valid: set[tuple[UUID, str]] = set()
        self.revoked_sessions: dict[str, int] = {}  # session id -> ttl it was revoked with

    async def revoke_session(self, *, session_id: str, ttl_seconds: int) -> None:
        self.revoked_sessions[session_id] = ttl_seconds

    async def is_session_revoked(self, *, session_id: str) -> bool:
        return session_id in self.revoked_sessions

    async def store_refresh_token(self, *, user_id: UUID, jti: str, expires_at: datetime) -> None:
        self.valid.add((user_id, jti))

    async def is_refresh_token_valid(self, *, user_id: UUID, jti: str) -> bool:
        return (user_id, jti) in self.valid

    async def revoke_refresh_token(self, *, user_id: UUID, jti: str) -> None:
        self.valid.discard((user_id, jti))

    async def consume_refresh_token(self, *, user_id: UUID, jti: str) -> bool:
        if (user_id, jti) not in self.valid:
            return False
        self.valid.discard((user_id, jti))
        return True

    async def revoke_all_for_user(self, *, user_id: UUID) -> None:
        self.valid = {pair for pair in self.valid if pair[0] != user_id}


class RecordingNotificationSender(NotificationSenderPort):
    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    async def send_email(self, *, to: str, subject: str, body: str) -> None:
        self.sent.append({"to": to, "subject": subject, "body": body})


class InMemoryPostsApiAdapter(PostsApiPort):
    """Stands in for `JsonPlaceholderPostsAdapter` in tests — unlike the real external API,
    this fake DOES persist writes across calls within a test, which is what exercising the
    use-case logic needs."""

    def __init__(self, seed: list[Post] | None = None) -> None:
        self.posts: dict[int, Post] = {p.id: p for p in (seed or [])}
        self._next_id = max(self.posts, default=100) + 1

    async def list_posts(self, params: OffsetParams) -> OffsetPage[Post]:
        items = list(self.posts.values())
        start = params.offset
        page_items = items[start : start + params.page_size]
        return OffsetPage(
            items=page_items, total=len(items), page=params.page, page_size=params.page_size
        )

    async def get_post(self, post_id: int) -> Post | None:
        return self.posts.get(post_id)

    async def create_post(self, *, user_id: int, title: str, body: str) -> Post:
        post = Post(id=self._next_id, user_id=user_id, title=title, body=body)
        self.posts[post.id] = post
        self._next_id += 1
        return post

    async def update_post(
        self, post_id: int, *, title: str | None, body: str | None
    ) -> Post | None:
        existing = self.posts.get(post_id)
        if existing is None:
            return None
        # A fresh object, like the real adapter builds from a response — never mutate
        # `existing` in place, or a caller holding an earlier `get_post()` reference (e.g.
        # the use case's `before` snapshot) would see this update retroactively.
        updated = Post(
            id=existing.id,
            user_id=existing.user_id,
            title=title if title is not None else existing.title,
            body=body if body is not None else existing.body,
        )
        self.posts[post_id] = updated
        return updated

    async def delete_post(self, post_id: int) -> None:
        self.posts.pop(post_id, None)


class FakeAsyncRedis:
    """A tiny in-memory stand-in for `redis.asyncio.Redis`, covering only
    the operations `CacheAside` uses."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def set(self, key: str, value: str, *, ex: int | None = None) -> None:
        self._store[key] = value

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self._store.pop(key, None)

    async def exists(self, key: str) -> bool:
        return key in self._store

    async def sadd(self, key: str, *values: str) -> None:
        pass

    async def srem(self, key: str, *values: str) -> None:
        pass

    async def expire(self, key: str, ttl: int) -> None:
        pass

    async def scan_iter(self, *, match: str) -> Any:
        for key in list(self._store):
            if fnmatch(key, match):
                yield key


class InMemoryUploadRepository(UploadRepositoryPort):
    def __init__(self) -> None:
        self.sessions: dict[UUID, UploadSession] = {}

    async def add_session(self, session: UploadSession) -> UploadSession:
        self.sessions[session.id] = session
        return session

    async def get_owned_session(self, upload_id: UUID, owner_id: UUID) -> UploadSession | None:
        session = self.sessions.get(upload_id)
        return session if session is not None and session.owner_id == owner_id else None

    async def get_session_unscoped(self, upload_id: UUID) -> UploadSession | None:
        return self.sessions.get(upload_id)

    async def count_active_sessions(self, owner_id: UUID, now: datetime) -> int:
        return sum(
            1
            for s in self.sessions.values()
            if s.owner_id == owner_id
            and s.status in (UploadStatus.INITIATED, UploadStatus.UPLOADING)
        )

    async def transition_session(
        self, upload_id: UUID, expected: UploadStatus, new: UploadStatus, **fields: Any
    ) -> bool:
        session = self.sessions.get(upload_id)
        if session is None or session.status is not expected:
            return False
        for name, value in fields.items():
            setattr(session, name, value)
        session.status = new
        return True

    async def list_sessions_by_status(
        self, statuses: Sequence[UploadStatus]
    ) -> list[UploadSession]:
        return [s for s in self.sessions.values() if s.status in statuses]


class InMemoryChunkRepository(ChunkRepositoryPort):
    def __init__(self) -> None:
        self.chunks: dict[tuple[UUID, int], UploadChunk] = {}

    async def record_chunk(self, chunk: UploadChunk) -> UploadChunk:
        key = (chunk.upload_id, chunk.index)
        if key in self.chunks:
            raise ChunkAlreadyReceivedError()
        self.chunks[key] = chunk
        return chunk

    async def get_chunk(self, upload_id: UUID, index: int) -> UploadChunk | None:
        return self.chunks.get((upload_id, index))

    async def list_chunks(self, upload_id: UUID, tenant_id: UUID) -> list[UploadChunk]:
        return [c for (uid, _), c in self.chunks.items() if uid == upload_id]


class InMemoryFileRepository(FileRepositoryPort):
    def __init__(self) -> None:
        self.files: dict[UUID, FileAsset] = {}
        self.variants: dict[UUID, list[FileVariant]] = {}
        self.purged: set[UUID] = set()

    async def add_file(self, asset: FileAsset) -> FileAsset:
        self.files[asset.id] = asset
        return asset

    async def get_owned_file(self, file_id: UUID) -> FileAsset | None:
        return self.files.get(file_id)

    async def get_file_unscoped(self, file_id: UUID) -> FileAsset | None:
        return self.files.get(file_id)

    async def transition_file(
        self, file_id: UUID, expected: FileStatus, new: FileStatus, **fields: Any
    ) -> bool:
        asset = self.files.get(file_id)
        if asset is None or asset.status is not expected:
            return False
        for name, value in fields.items():
            setattr(asset, name, value)
        asset.status = new
        return True

    async def add_variants(
        self, file_id: UUID, tenant_id: UUID, variants: list[FileVariant]
    ) -> None:
        self.variants.setdefault(file_id, []).extend(variants)

    async def list_variants(
        self, file_id: UUID, tenant_id: UUID | None = None
    ) -> list[FileVariant]:
        return self.variants.get(file_id, [])

    async def list_files_by_status(self, statuses: Sequence[FileStatus]) -> list[FileAsset]:
        return [a for a in self.files.values() if a.status in statuses]

    async def list_unpurged_files(self, statuses: Sequence[FileStatus]) -> list[FileAsset]:
        return [a for a in self.files.values() if a.status in statuses and a.id not in self.purged]

    async def mark_storage_purged(self, file_id: UUID, now: datetime) -> None:
        self.purged.add(file_id)


class InMemoryObjectStorage(ObjectStoragePort):
    """Dict-backed stand-in: `StorageKey` is a frozen, hashable dataclass, so it is used
    directly as the dict key."""

    def __init__(self) -> None:
        self.blobs: dict[StorageKey, bytes] = {}

    async def write(self, key: StorageKey, source: AsyncIterator[bytes]) -> None:
        data = b""
        async for block in source:
            data += block
        self.blobs[key] = data

    async def read(
        self, key: StorageKey, *, chunk_size: int = 1024 * 1024
    ) -> AsyncGenerator[bytes]:
        data = self.blobs.get(key, b"")
        for i in range(0, len(data), chunk_size):
            yield data[i : i + chunk_size]

    async def exists(self, key: StorageKey) -> bool:
        return key in self.blobs

    async def move(self, source: StorageKey, destination: StorageKey) -> None:
        self.blobs[destination] = self.blobs.pop(source)

    async def concatenate(
        self, sources: Sequence[StorageKey], destination: StorageKey
    ) -> tuple[int, str]:
        import hashlib

        data = b"".join(self.blobs[s] for s in sources)
        self.blobs[destination] = data
        return len(data), hashlib.sha256(data).hexdigest()

    async def delete_tree(self, key: StorageKey) -> None:
        prefix_len = len(key.parts)
        for stale in [
            k for k in self.blobs if k.region == key.region and k.parts[:prefix_len] == key.parts
        ]:
            del self.blobs[stale]

    async def list_children(self, key: StorageKey) -> list[str]:
        prefix_len = len(key.parts)
        names = {
            k.parts[prefix_len]
            for k in self.blobs
            if k.region == key.region
            and len(k.parts) > prefix_len
            and k.parts[:prefix_len] == key.parts
        }
        return sorted(names)

    async def modified_at(self, key: StorageKey) -> datetime | None:
        return None

    @asynccontextmanager
    async def materialize(self, key: StorageKey) -> AsyncGenerator[Path]:
        # Not exercised by this plan's unit tests (only the files pipeline, not
        # UploadSingleFileUseCase/DeleteFileUseCase, calls materialize()); blocking I/O is
        # fine for a single-threaded test fixture.
        import os
        import tempfile

        fd, name = tempfile.mkstemp()
        path = Path(name)
        try:
            path.write_bytes(self.blobs.get(key, b""))  # noqa: ASYNC240
            yield path
        finally:
            os.close(fd)
            path.unlink(missing_ok=True)  # noqa: ASYNC240
