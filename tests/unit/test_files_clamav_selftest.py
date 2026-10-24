import asyncio
import struct

import pytest

from app.modules.files.domain.ports import ScannerUnavailableError
from app.modules.files.infrastructure.scanner import _SELF_TEST_PAYLOAD, ClamAVScanner


async def _fake_clamd(reply: bytes, received: list[bytes]) -> asyncio.Server:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        assert await reader.readuntil(b"\0") == b"zINSTREAM\0"
        data = b""
        while True:
            (length,) = struct.unpack(">I", await reader.readexactly(4))
            if length == 0:
                break
            data += await reader.readexactly(length)
        received.append(data)
        writer.write(reply)
        await writer.drain()
        writer.close()

    return await asyncio.start_server(handle, "127.0.0.1", 0)


async def _self_test_against(reply: bytes) -> tuple[list[bytes], Exception | None]:
    received: list[bytes] = []
    server = await _fake_clamd(reply, received)
    port = server.sockets[0].getsockname()[1]
    try:
        await ClamAVScanner("127.0.0.1", port, timeout_seconds=5).self_test()
    except ScannerUnavailableError as exc:
        return received, exc
    finally:
        server.close()
        await server.wait_closed()
    return received, None


async def test_self_test_scans_a_harmless_sample_and_accepts_ok() -> None:
    received, error = await _self_test_against(b"stream: OK\0")
    assert error is None
    assert received == [_SELF_TEST_PAYLOAD]


async def test_self_test_rejects_a_database_that_failed_to_load() -> None:
    # PING would still answer PONG here; the scan is what shows the signatures are unusable.
    _, error = await _self_test_against(b"stream: Can't open or parse the database ERROR\0")
    assert isinstance(error, ScannerUnavailableError)


async def test_self_test_treats_a_signature_hit_on_the_sample_as_a_fault() -> None:
    _, error = await _self_test_against(b"stream: Some-Signature FOUND\0")
    assert isinstance(error, ScannerUnavailableError)


async def test_self_test_unreachable_raises_unavailable() -> None:
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()  # nothing listens on this port any more
    with pytest.raises(ScannerUnavailableError):
        await ClamAVScanner("127.0.0.1", port, timeout_seconds=2).self_test()
