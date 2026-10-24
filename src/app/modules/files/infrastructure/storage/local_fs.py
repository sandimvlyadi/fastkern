"""Local filesystem implementation of `ObjectStoragePort`.

This is the only code that turns a `StorageKey` into a disk path. Every path is resolved and
checked to stay inside its region root. Blocking file I/O runs in worker threads, block by block,
so large files are streamed and never read whole into memory.
"""

import asyncio
import hashlib
import os
import shutil
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from app.modules.files.domain.ports import ObjectStoragePort
from app.modules.files.domain.value_objects import StorageKey

_BLOCK = 1024 * 1024
_FILE_MODE = 0o640
_DIR_MODE = 0o750


class LocalFileSystemStorage(ObjectStoragePort):
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def _path(self, key: StorageKey) -> Path:
        region_root = (self._root / key.region.value).resolve()
        path = region_root.joinpath(*key.parts).resolve()
        if not path.is_relative_to(
            region_root
        ):  # defense in depth; StorageKey already validates parts
            raise ValueError(f"storage path escapes its region: {key}")
        return path

    async def write(self, key: StorageKey, source: AsyncIterator[bytes]) -> None:
        path = self._path(key)
        await asyncio.to_thread(_ensure_dirs, path.parent, self._root)
        temp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            handle = await asyncio.to_thread(temp.open, "xb")
            try:
                async for block in source:
                    await asyncio.to_thread(handle.write, block)
            finally:
                await asyncio.to_thread(handle.close)
            await asyncio.to_thread(os.chmod, temp, _FILE_MODE)
            await asyncio.to_thread(os.replace, temp, path)
        except BaseException:
            await asyncio.to_thread(_unlink, temp)
            raise

    async def read(self, key: StorageKey, *, chunk_size: int = _BLOCK) -> AsyncGenerator[bytes]:
        handle = await asyncio.to_thread(self._path(key).open, "rb")
        try:
            while block := await asyncio.to_thread(handle.read, chunk_size):
                yield block
        finally:
            await asyncio.to_thread(handle.close)

    async def exists(self, key: StorageKey) -> bool:
        return await asyncio.to_thread(self._path(key).exists)

    async def move(self, source: StorageKey, destination: StorageKey) -> None:
        dst = self._path(destination)
        await asyncio.to_thread(_ensure_dirs, dst.parent, self._root)
        await asyncio.to_thread(os.replace, self._path(source), dst)

    async def concatenate(
        self, sources: Sequence[StorageKey], destination: StorageKey
    ) -> tuple[int, str]:
        path = self._path(destination)
        await asyncio.to_thread(_ensure_dirs, path.parent, self._root)
        temp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        digest = hashlib.sha256()
        size = 0
        try:
            handle = await asyncio.to_thread(temp.open, "xb")
            try:
                for source in sources:
                    async for block in self.read(source):
                        digest.update(block)
                        size += len(block)
                        await asyncio.to_thread(handle.write, block)
            finally:
                await asyncio.to_thread(handle.close)
            await asyncio.to_thread(os.chmod, temp, _FILE_MODE)
            await asyncio.to_thread(os.replace, temp, path)
        except BaseException:
            await asyncio.to_thread(_unlink, temp)
            raise
        return size, digest.hexdigest()

    async def delete_tree(self, key: StorageKey) -> None:
        await asyncio.to_thread(_remove, self._path(key))

    async def list_children(self, key: StorageKey) -> list[str]:
        return await asyncio.to_thread(_children, self._path(key))

    async def modified_at(self, key: StorageKey) -> datetime | None:
        return await asyncio.to_thread(_mtime, self._path(key))

    def materialize(self, key: StorageKey) -> AbstractAsyncContextManager[Path]:
        # Local storage already has a path; remote adapters would download to a temp file here.
        @asynccontextmanager
        async def _local() -> AsyncIterator[Path]:
            yield self._path(key)

        return _local()


def _ensure_dirs(directory: Path, root: Path) -> None:
    """Create `directory` under `root` and every missing level, then set `_DIR_MODE` on every level
    from `directory` up to `root`.

    `Path.mkdir(mode=...)` applies the mode to the leaf only, and the umask decides the rest, so
    intermediate directories would come out 0o755. Re-applying the mode on each write also repairs
    directories created with the wrong mode, such as on an existing `files_data` volume."""
    root.mkdir(parents=True, exist_ok=True)
    levels: list[Path] = []
    current = directory
    while True:
        levels.append(current)
        if current == root or current.parent == current:
            break
        current = current.parent
    for candidate in reversed(levels):
        candidate.mkdir(exist_ok=True)
        os.chmod(candidate, _DIR_MODE)


def _unlink(path: Path) -> None:
    path.unlink(missing_ok=True)


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


def _mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except FileNotFoundError:
        return None


def _children(path: Path) -> list[str]:
    if not path.is_dir():
        return []
    return sorted(child.name for child in path.iterdir())
