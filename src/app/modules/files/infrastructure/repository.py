"""Repositories for the files module.

Reads go through `BaseRepository._base_query()` (tenant + soft-delete scoping). Three deliberate
exceptions, documented on the ports: background tasks and cleanup have no request tenant, so they
pass `tenant_id` explicitly or use the `*_unscoped` / `_by_status` methods; and status changes are
conditional UPDATEs by primary key, the only way to make a transition atomic.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.files.domain.entities import FileAsset, FileVariant, UploadChunk, UploadSession
from app.modules.files.domain.ports import (
    ChunkAlreadyReceivedError,
    ChunkRepositoryPort,
    FileRepositoryPort,
    UploadRepositoryPort,
)
from app.modules.files.domain.state import FileStatus, UploadStatus
from app.modules.files.domain.value_objects import FileKind, Visibility
from app.modules.files.infrastructure.models import (
    CHUNK_UNIQUE_INDEX,
    FileModel,
    FileUploadChunkModel,
    FileUploadModel,
    FileVariantModel,
)
from app.shared.infrastructure.integrity import violated_unique_constraint
from app.shared.infrastructure.repository import BaseRepository


class UploadRepository(BaseRepository[FileUploadModel, UploadSession], UploadRepositoryPort):
    model = FileUploadModel

    def _to_entity(self, row: FileUploadModel) -> UploadSession:
        return UploadSession(
            id=row.id,
            tenant_id=row.tenant_id,
            owner_id=row.created_by,
            filename=row.filename_display,
            declared_size=row.declared_size,
            chunk_size=row.chunk_size,
            total_chunks=row.total_chunks,
            expected_sha256=row.expected_sha256,
            visibility=Visibility(row.visibility),
            status=UploadStatus(row.status),
            expires_at=row.expires_at,
            file_id=row.file_id,
            failure_reason=row.failure_reason,
        )

    def _to_model(self, entity: UploadSession) -> FileUploadModel:
        return FileUploadModel(
            id=entity.id,
            tenant_id=entity.tenant_id,
            created_by=entity.owner_id,
            filename_display=entity.filename,
            declared_size=entity.declared_size,
            chunk_size=entity.chunk_size,
            total_chunks=entity.total_chunks,
            expected_sha256=entity.expected_sha256,
            visibility=entity.visibility.value,
            status=entity.status.value,
            expires_at=entity.expires_at,
            file_id=entity.file_id,
            failure_reason=entity.failure_reason,
        )

    async def add_session(self, session: UploadSession) -> UploadSession:
        return await self.add(session)

    async def get_owned_session(self, upload_id: UUID, owner_id: UUID) -> UploadSession | None:
        stmt = self._base_query().where(
            FileUploadModel.id == upload_id,
            FileUploadModel.created_by == owner_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return self._to_entity(row) if row is not None else None

    async def get_session_unscoped(self, upload_id: UUID) -> UploadSession | None:
        stmt = self._base_query(tenant_scoped=False).where(FileUploadModel.id == upload_id)
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return self._to_entity(row) if row is not None else None

    async def count_active_sessions(self, owner_id: UUID, now: datetime) -> int:
        active = self._base_query().where(
            FileUploadModel.created_by == owner_id,
            FileUploadModel.status.in_(
                [UploadStatus.INITIATED.value, UploadStatus.UPLOADING.value]
            ),
            FileUploadModel.expires_at > now,
        )
        return int(
            (
                await self._session.execute(select(func.count()).select_from(active.subquery()))
            ).scalar_one()
        )

    async def transition_session(
        self, upload_id: UUID, expected: UploadStatus, new: UploadStatus, **fields: Any
    ) -> bool:
        stmt = (
            update(FileUploadModel)
            .where(FileUploadModel.id == upload_id, FileUploadModel.status == expected.value)
            .values(status=new.value, **fields)
            .execution_options(synchronize_session="fetch")
        )
        result = cast(CursorResult[Any], await self._session.execute(stmt))
        return bool(result.rowcount == 1)

    async def list_sessions_by_status(
        self, statuses: Sequence[UploadStatus]
    ) -> list[UploadSession]:
        stmt = self._base_query(tenant_scoped=False).where(
            FileUploadModel.status.in_([s.value for s in statuses])
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [self._to_entity(row) for row in rows]


class ChunkRepository(BaseRepository[FileUploadChunkModel, UploadChunk], ChunkRepositoryPort):
    model = FileUploadChunkModel

    def _to_entity(self, row: FileUploadChunkModel) -> UploadChunk:
        return UploadChunk(
            id=row.id,
            tenant_id=row.tenant_id,
            upload_id=row.upload_id,
            index=row.chunk_index,
            size_bytes=row.size_bytes,
            sha256=row.sha256,
        )

    def _to_model(self, entity: UploadChunk) -> FileUploadChunkModel:
        return FileUploadChunkModel(
            id=entity.id,
            tenant_id=entity.tenant_id,
            upload_id=entity.upload_id,
            chunk_index=entity.index,
            size_bytes=entity.size_bytes,
            sha256=entity.sha256,
        )

    async def record_chunk(self, chunk: UploadChunk) -> UploadChunk:
        """Insert-first race guard: the unique (upload, index) row decides the winner before the
        temp file is renamed into place. A failed flush rolls the session back (BaseRepository's
        convention), so callers must re-read anything they need afterwards."""
        model = self._to_model(chunk)
        actor_id = self._current_actor()
        if actor_id is not None:
            model.created_by = actor_id
            model.updated_by = actor_id
        self._session.add(model)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            if violated_unique_constraint(exc) == CHUNK_UNIQUE_INDEX:
                raise ChunkAlreadyReceivedError from exc
            raise
        return self._to_entity(model)

    async def get_chunk(self, upload_id: UUID, index: int) -> UploadChunk | None:
        stmt = self._base_query().where(
            FileUploadChunkModel.upload_id == upload_id,
            FileUploadChunkModel.chunk_index == index,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return self._to_entity(row) if row is not None else None

    async def list_chunks(self, upload_id: UUID, tenant_id: UUID) -> list[UploadChunk]:
        stmt = (
            self._base_query(tenant_id=tenant_id)
            .where(FileUploadChunkModel.upload_id == upload_id)
            .order_by(FileUploadChunkModel.chunk_index)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [self._to_entity(row) for row in rows]


class VariantRepository(BaseRepository[FileVariantModel, FileVariant]):
    model = FileVariantModel

    def _to_entity(self, row: FileVariantModel) -> FileVariant:
        return FileVariant(
            name=row.name,
            width=row.width,
            height=row.height,
            size_bytes=row.size_bytes,
            sha256=row.sha256,
            mime_type=row.mime_type,
        )

    def _to_model(self, entity: FileVariant) -> FileVariantModel:
        # Variants are written through `add_all`, which needs the parent file id and tenant.
        raise NotImplementedError("use add_all(file_id, tenant_id, variants)")

    async def add_all(self, file_id: UUID, tenant_id: UUID, variants: list[FileVariant]) -> None:
        for variant in variants:
            self._session.add(
                FileVariantModel(
                    tenant_id=tenant_id,
                    file_id=file_id,
                    name=variant.name,
                    width=variant.width,
                    height=variant.height,
                    size_bytes=variant.size_bytes,
                    sha256=variant.sha256,
                    mime_type=variant.mime_type,
                )
            )
        await self._session.flush()

    async def list_for_file(
        self, file_id: UUID, tenant_id: UUID | None = None
    ) -> list[FileVariant]:
        stmt = (
            self._base_query(tenant_id=tenant_id)
            .where(FileVariantModel.file_id == file_id)
            .order_by(FileVariantModel.name)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [self._to_entity(row) for row in rows]


class FileRepository(BaseRepository[FileModel, FileAsset], FileRepositoryPort):
    model = FileModel

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self._variants = VariantRepository(session)

    def _to_entity(self, row: FileModel) -> FileAsset:
        return FileAsset(
            id=row.id,
            tenant_id=row.tenant_id,
            owner_id=row.created_by,
            status=FileStatus(row.status),
            visibility=Visibility(row.visibility),
            kind=FileKind(row.kind),
            mime_type=row.mime_type,
            size_bytes=row.size_bytes,
            sha256=row.sha256,
            width=row.width,
            height=row.height,
            original_name=row.original_name,
            failure_reason=row.failure_reason,
            created_at=row.created_at,
        )

    def _to_model(self, entity: FileAsset) -> FileModel:
        return FileModel(
            id=entity.id,
            tenant_id=entity.tenant_id,
            created_by=entity.owner_id,
            status=entity.status.value,
            visibility=entity.visibility.value,
            kind=entity.kind.value,
            mime_type=entity.mime_type,
            size_bytes=entity.size_bytes,
            sha256=entity.sha256,
            width=entity.width,
            height=entity.height,
            original_name=entity.original_name,
            failure_reason=entity.failure_reason,
        )

    async def add_file(self, asset: FileAsset) -> FileAsset:
        return await self.add(asset)

    async def get_owned_file(self, file_id: UUID) -> FileAsset | None:
        return await self.get_by_id(file_id)

    async def get_file_unscoped(self, file_id: UUID) -> FileAsset | None:
        return await self.get_by_id(file_id, tenant_scoped=False)

    async def transition_file(
        self, file_id: UUID, expected: FileStatus, new: FileStatus, **fields: Any
    ) -> bool:
        stmt = (
            update(FileModel)
            .where(FileModel.id == file_id, FileModel.status == expected.value)
            .values(status=new.value, **fields)
            .execution_options(synchronize_session="fetch")
        )
        result = cast(CursorResult[Any], await self._session.execute(stmt))
        return bool(result.rowcount == 1)

    async def add_variants(
        self, file_id: UUID, tenant_id: UUID, variants: list[FileVariant]
    ) -> None:
        await self._variants.add_all(file_id, tenant_id, variants)

    async def list_variants(
        self, file_id: UUID, tenant_id: UUID | None = None
    ) -> list[FileVariant]:
        return await self._variants.list_for_file(file_id, tenant_id)

    async def list_files_by_status(self, statuses: Sequence[FileStatus]) -> list[FileAsset]:
        stmt = self._base_query(tenant_scoped=False, include_deleted=True).where(
            FileModel.status.in_([s.value for s in statuses])
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [self._to_entity(row) for row in rows]

    async def list_unpurged_files(self, statuses: Sequence[FileStatus]) -> list[FileAsset]:
        stmt = self._base_query(tenant_scoped=False, include_deleted=True).where(
            FileModel.status.in_([s.value for s in statuses]),
            FileModel.storage_purged_at.is_(None),
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [self._to_entity(row) for row in rows]

    async def mark_storage_purged(self, file_id: UUID, now: datetime) -> None:
        await self._session.execute(
            update(FileModel)
            .where(FileModel.id == file_id)
            .values(storage_purged_at=now)
            .execution_options(synchronize_session="fetch")
        )
