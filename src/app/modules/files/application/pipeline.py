"""Background pipeline for an uploaded file: assemble → detect → scan → (image) process → publish.

Each step is a conditional status transition, so a re-delivered task cannot repeat or skip work.
Controlled failures end in REJECTED (the content is unacceptable) or FAILED (the system could not
handle it). Either way the file's artifacts are removed. The Celery task that runs this is in
`infrastructure/tasks.py`.
"""

import asyncio
import shutil
import tempfile
from collections.abc import AsyncIterator, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from app.modules.files.domain import signatures
from app.modules.files.domain.entities import FileAsset, FileVariant, UploadSession
from app.modules.files.domain.exceptions import FileRejectedError
from app.modules.files.domain.ports import (
    ChunkRepositoryPort,
    FileRepositoryPort,
    ImageOptions,
    ImageProcessorPort,
    MalwareScannerPort,
    ObjectStoragePort,
    ScannerUnavailableError,
    UploadRepositoryPort,
)
from app.modules.files.domain.signatures import DetectedType
from app.modules.files.domain.state import FileStatus
from app.modules.files.domain.value_objects import (
    FileKind,
    Region,
    StorageKey,
    Visibility,
    file_key,
    upload_chunk_key,
    upload_dir_key,
)

_HEAD_BYTES = 64
_BLOCK = 1024 * 1024
# Rejections for the content itself. Anything else is a FAILED, i.e. a system problem.
_CONTENT_REASONS = frozenset(
    {"MALWARE", "DISALLOWED_TYPE", "INVALID_IMAGE", "TYPE_MISMATCH", "IMAGE_TOO_LARGE"}
)


class FileNotYetVisibleError(Exception):
    """The file row is not committed yet (the completing request has not finished). Retry later."""


@dataclass(slots=True)
class _Progress:
    status: FileStatus


async def _file_blocks(path: Path) -> AsyncIterator[bytes]:
    handle = await asyncio.to_thread(path.open, "rb")
    try:
        while block := await asyncio.to_thread(handle.read, _BLOCK):
            yield block
    finally:
        await asyncio.to_thread(handle.close)


class ProcessUploadUseCase:
    def __init__(
        self,
        uploads: UploadRepositoryPort,
        chunks: ChunkRepositoryPort,
        files: FileRepositoryPort,
        storage: ObjectStoragePort,
        scanner: MalwareScannerPort,
        processor: ImageProcessorPort,
        options: ImageOptions,
        work_dir: Path,
    ) -> None:
        self._uploads = uploads
        self._chunks = chunks
        self._files = files
        self._storage = storage
        self._scanner = scanner
        self._processor = processor
        self._options = options
        self._work_dir = work_dir

    async def execute(self, upload_id: UUID, file_id: UUID) -> str:
        session = await self._uploads.get_session_unscoped(upload_id)
        asset = await self._files.get_file_unscoped(file_id)
        if session is None or asset is None:
            raise FileNotYetVisibleError()
        if asset.status is not FileStatus.UPLOADED:
            return asset.status.value  # already handled by an earlier delivery

        progress = _Progress(status=FileStatus.UPLOADED)
        source = file_key(Region.PRIVATE, file_id, "source")
        try:
            await self._run(session, asset, progress, source)
        except Exception:
            # Anything unexpected must not leave the file stuck in a non-terminal state.
            await self._fail(asset.id, progress, "PROCESSING_ERROR")
            raise
        finally:
            await self._storage.delete_tree(upload_dir_key(upload_id))
        return progress.status.value

    async def _run(
        self, session: UploadSession, asset: FileAsset, progress: _Progress, source: StorageKey
    ) -> None:
        try:
            size, sha = await self._assemble(session, asset.tenant_id, source)
            await self._advance(
                asset.id, progress, FileStatus.SCANNING, size_bytes=size, sha256=sha
            )
            detected = await self._detect(source)
            await self._scan(source)
            target = Region.PUBLIC if session.visibility is Visibility.PUBLIC else Region.PRIVATE
            if detected.kind is FileKind.IMAGE:
                await self._publish_image(asset, progress, source, detected, target)
            else:
                await self._publish_original(asset, progress, source, detected, target, size, sha)
            await self._storage.delete_tree(source)  # no-op when the source was moved
        except FileRejectedError as exc:
            await self._fail(asset.id, progress, exc.reason)

    async def _publish_image(
        self,
        asset: FileAsset,
        progress: _Progress,
        source: StorageKey,
        detected: DetectedType,
        target: Region,
    ) -> None:
        await self._advance(asset.id, progress, FileStatus.PROCESSING, kind=detected.kind.value)
        variants, width, height = await self._render(source, asset, detected, target)
        await self._files.add_variants(asset.id, asset.tenant_id, variants)
        # The stored master is the re-encoded WebP, not the uploaded format, so the file's mime
        # describes what is actually stored (the variant list's `original` entry).
        await self._advance(
            asset.id,
            progress,
            FileStatus.AVAILABLE,
            width=width,
            height=height,
            mime_type=variants[0].mime_type,
        )

    async def _publish_original(
        self,
        asset: FileAsset,
        progress: _Progress,
        source: StorageKey,
        detected: DetectedType,
        target: Region,
        size: int,
        sha: str,
    ) -> None:
        # Documents and generic files keep their bytes as the single `original` variant.
        await self._storage.move(source, file_key(target, asset.id, "variants", "original"))
        await self._files.add_variants(
            asset.id,
            asset.tenant_id,
            [
                FileVariant(
                    name="original",
                    size_bytes=size,
                    sha256=sha,
                    mime_type=detected.mime_type,
                )
            ],
        )
        await self._advance(
            asset.id,
            progress,
            FileStatus.AVAILABLE,
            kind=detected.kind.value,
            mime_type=detected.mime_type,
        )

    async def _assemble(
        self, session: UploadSession, tenant_id: UUID, destination: StorageKey
    ) -> tuple[int, str]:
        received = {chunk.index for chunk in await self._chunks.list_chunks(session.id, tenant_id)}
        if any(index not in received for index in range(session.total_chunks)):
            raise FileRejectedError("CHUNK_MISSING")
        sources: Sequence[StorageKey] = [
            upload_chunk_key(session.id, index) for index in range(session.total_chunks)
        ]
        try:
            size, sha = await self._storage.concatenate(sources, destination)
        except FileNotFoundError as exc:
            raise FileRejectedError("CHUNK_MISSING") from exc
        if size != session.declared_size:
            raise FileRejectedError("SIZE_MISMATCH")
        if session.expected_sha256 is not None and sha != session.expected_sha256:
            raise FileRejectedError("CHECKSUM_MISMATCH")
        return size, sha

    async def _detect(self, source: StorageKey) -> DetectedType:
        head = b""
        async with aclosing(self._storage.read(source, chunk_size=_HEAD_BYTES)) as blocks:
            async for block in blocks:
                head += block
                if len(head) >= _HEAD_BYTES:
                    break
        detected = signatures.detect(head)
        if detected is None:
            raise FileRejectedError("DISALLOWED_TYPE")
        return detected

    async def _scan(self, source: StorageKey) -> None:
        try:
            async with self._storage.materialize(source) as local:
                result = await self._scanner.scan(local)
        except ScannerUnavailableError as exc:
            raise FileRejectedError("SCAN_UNAVAILABLE") from exc
        if not result.clean:
            raise FileRejectedError("MALWARE")

    async def _render(
        self, source: StorageKey, asset: FileAsset, detected: DetectedType, target: Region
    ) -> tuple[list[FileVariant], int, int]:
        work = Path(tempfile.mkdtemp(prefix=f"{asset.id}-", dir=self._work_dir))
        try:
            async with self._storage.materialize(source) as local:
                result = await asyncio.to_thread(
                    self._processor.process, local, work, self._options, detected.image_format or ""
                )
            variants: list[FileVariant] = []
            for output in result.outputs:
                await self._storage.write(
                    file_key(target, asset.id, "variants", output.name),
                    _file_blocks(output.path),
                )
                variants.append(
                    FileVariant(
                        name=output.name,
                        width=output.width,
                        height=output.height,
                        size_bytes=output.size_bytes,
                        sha256=output.sha256,
                        mime_type=output.mime_type,
                    )
                )
            return variants, result.width, result.height
        finally:
            shutil.rmtree(work, ignore_errors=True)

    async def _advance(
        self, file_id: UUID, progress: _Progress, new: FileStatus, **fields: object
    ) -> None:
        moved = await self._files.transition_file(file_id, progress.status, new, **fields)
        if not moved:
            raise RuntimeError(
                f"file {file_id} changed status concurrently ({progress.status} -> {new})"
            )
        progress.status = new

    async def _fail(self, file_id: UUID, progress: _Progress, reason: str) -> None:
        target = (
            FileStatus.REJECTED
            if reason in _CONTENT_REASONS and progress.status is not FileStatus.UPLOADED
            else FileStatus.FAILED
        )
        await self._files.transition_file(file_id, progress.status, target, failure_reason=reason)
        progress.status = target
        for region in (Region.PRIVATE, Region.PUBLIC):
            await self._storage.delete_tree(file_key(region, file_id))
