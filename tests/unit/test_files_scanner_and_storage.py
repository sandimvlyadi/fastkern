import asyncio
import hashlib
import struct
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.modules.files.domain.ports import ScannerUnavailableError
from app.modules.files.domain.value_objects import Region, StorageKey
from app.modules.files.infrastructure.scanner import ClamAVScanner, NoopScanner
from app.modules.files.infrastructure.storage.local_fs import LocalFileSystemStorage


async def _fake_clamd(received: list[bytes], verdict: bytes) -> asyncio.Server:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        assert await reader.readexactly(10) == b"zINSTREAM\0"
        data = b""
        while True:
            (length,) = struct.unpack(">I", await reader.readexactly(4))
            if length == 0:
                break
            data += await reader.readexactly(length)
        received.append(data)
        writer.write(verdict)
        await writer.drain()
        writer.close()

    return await asyncio.start_server(handle, "127.0.0.1", 0)


async def test_clamav_streams_the_file_and_reports_clean(tmp_path: Path) -> None:
    sample = tmp_path / "sample.bin"
    payload = b"harmless" * 50_000  # spans several INSTREAM frames
    sample.write_bytes(payload)
    received: list[bytes] = []
    server = await _fake_clamd(received, b"stream: OK\0")
    port = server.sockets[0].getsockname()[1]
    try:
        result = await ClamAVScanner("127.0.0.1", port, timeout_seconds=5).scan(sample)
    finally:
        server.close()
        await server.wait_closed()
    assert result.clean is True
    assert received == [payload]


async def test_clamav_reports_infected_with_signature(tmp_path: Path) -> None:
    sample = tmp_path / "eicar.bin"
    sample.write_bytes(b"X5O!P%@AP")
    server = await _fake_clamd([], b"stream: Eicar-Test-Signature FOUND\0")
    port = server.sockets[0].getsockname()[1]
    try:
        result = await ClamAVScanner("127.0.0.1", port, timeout_seconds=5).scan(sample)
    finally:
        server.close()
        await server.wait_closed()
    assert result.clean is False
    assert result.signature == "Eicar-Test-Signature"


async def test_clamav_unreachable_raises_unavailable(tmp_path: Path) -> None:
    sample = tmp_path / "x.bin"
    sample.write_bytes(b"data")
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()  # nothing listens on this port any more
    with pytest.raises(ScannerUnavailableError):
        await ClamAVScanner("127.0.0.1", port, timeout_seconds=2).scan(sample)


async def test_noop_scanner_is_clean(tmp_path: Path) -> None:
    sample = tmp_path / "x.bin"
    sample.write_bytes(b"anything")
    assert (await NoopScanner().scan(sample)).clean is True


async def _blocks(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


async def test_storage_write_read_roundtrip_and_permissions(tmp_path: Path) -> None:
    storage = LocalFileSystemStorage(tmp_path)
    key = StorageKey(Region.PRIVATE, ("files", "abc", "source"))
    await storage.write(key, _blocks(b"hello ", b"world"))

    assert [block async for block in storage.read(key)] == [b"hello world"]
    path = tmp_path / "private" / "files" / "abc" / "source"
    assert oct(path.stat().st_mode)[-3:] == "640"
    # Every directory level is 0o750, not only the leaf (mkdir's mode leaves parents at 0o755).
    for level in (tmp_path / "private", tmp_path / "private" / "files", path.parent):
        assert oct(level.stat().st_mode)[-3:] == "750", level
    assert not any(p.name.endswith(".tmp") for p in path.parent.iterdir())


async def test_failed_write_leaves_nothing_behind(tmp_path: Path) -> None:
    storage = LocalFileSystemStorage(tmp_path)
    key = StorageKey(Region.TMP, ("uploads", "u1", "chunks", "000000.part"))

    async def broken() -> AsyncIterator[bytes]:
        yield b"partial"
        raise RuntimeError("client disconnected")

    with pytest.raises(RuntimeError):
        await storage.write(key, broken())
    assert await storage.exists(key) is False
    assert await storage.list_children(StorageKey(Region.TMP, ("uploads", "u1", "chunks"))) == []


async def test_concatenate_returns_size_and_sha256_in_order(tmp_path: Path) -> None:
    storage = LocalFileSystemStorage(tmp_path)
    parts = [StorageKey(Region.TMP, ("u", f"{i}.part")) for i in range(3)]
    for part, data in zip(parts, [b"aa", b"bb", b"cc"], strict=True):
        await storage.write(part, _blocks(data))
    dst = StorageKey(Region.PRIVATE, ("files", "f", "source"))

    size, sha = await storage.concatenate(parts, dst)

    assert size == 6
    assert sha == hashlib.sha256(b"aabbcc").hexdigest()
    assert [b async for b in storage.read(dst)] == [b"aabbcc"]


async def test_concatenate_with_a_missing_part_raises_file_not_found(tmp_path: Path) -> None:
    storage = LocalFileSystemStorage(tmp_path)
    present = StorageKey(Region.TMP, ("u", "0.part"))
    await storage.write(present, _blocks(b"x"))
    dst = StorageKey(Region.PRIVATE, ("files", "f", "source"))
    with pytest.raises(FileNotFoundError):
        await storage.concatenate([present, StorageKey(Region.TMP, ("u", "1.part"))], dst)
    assert await storage.exists(dst) is False


async def test_delete_tree_is_idempotent(tmp_path: Path) -> None:
    storage = LocalFileSystemStorage(tmp_path)
    folder = StorageKey(Region.PUBLIC, ("files", "f1"))
    await storage.write(folder.child("variants", "256"), _blocks(b"x"))
    await storage.delete_tree(folder)
    await storage.delete_tree(folder)  # second call: nothing left, still fine
    assert await storage.list_children(StorageKey(Region.PUBLIC, ("files",))) == []


async def test_move_is_atomic_across_regions(tmp_path: Path) -> None:
    storage = LocalFileSystemStorage(tmp_path)
    src = StorageKey(Region.PRIVATE, ("files", "f", "source"))
    dst = StorageKey(Region.PUBLIC, ("files", "f", "variants", "original"))
    await storage.write(src, _blocks(b"pdf-bytes"))
    await storage.move(src, dst)
    assert await storage.exists(src) is False
    assert [b async for b in storage.read(dst)] == [b"pdf-bytes"]


async def test_existing_directories_are_repaired_on_the_next_write(tmp_path: Path) -> None:
    """A directory created before the mode fix (or a pre-existing volume) is brought to 0o750."""
    storage = LocalFileSystemStorage(tmp_path)
    legacy = tmp_path / "private" / "files"
    legacy.mkdir(parents=True)
    legacy.chmod(0o755)
    await storage.write(StorageKey(Region.PRIVATE, ("files", "f", "source")), _blocks(b"x"))
    assert oct(legacy.stat().st_mode)[-3:] == "750"


async def test_clamav_reply_without_a_terminator_is_refused(tmp_path: Path) -> None:
    """The reply is read as one bounded NUL-terminated line; an unbounded one is unavailable."""
    sample = tmp_path / "x.bin"
    sample.write_bytes(b"data")

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readexactly(10)  # the zINSTREAM command, then the client's frames
        writer.write(b"x" * 200_000)  # 200 KB with no NUL terminator
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        with pytest.raises(ScannerUnavailableError):
            await ClamAVScanner("127.0.0.1", port, timeout_seconds=5).scan(sample)
    finally:
        server.close()
        await server.wait_closed()
