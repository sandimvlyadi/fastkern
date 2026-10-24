"""Malware scanner adapters. ClamAV is optional (MALWARE_SCAN_BACKEND=clamav, default off).

The ClamAV adapter speaks clamd's INSTREAM protocol directly over asyncio, so it adds no pip
dependency.
"""

import asyncio
import struct
from collections.abc import Awaitable, Callable
from pathlib import Path

from app.modules.files.domain.ports import MalwareScannerPort, ScannerUnavailableError, ScanResult

_READ_BLOCK = 64 * 1024
# Harmless and never matched by a signature. Scanning it proves that clamd is up AND that its
# signature database loaded: a clamd whose database failed to load errors on scans, not on a ping.
_SELF_TEST_PAYLOAD = b"fastkern clamd self-test\n"

Sender = Callable[[asyncio.StreamWriter], Awaitable[None]]


class NoopScanner(MalwareScannerPort):
    """Used when malware scanning is disabled. Never reports a file as infected."""

    async def scan(self, path: Path) -> ScanResult:
        return ScanResult(clean=True)

    async def self_test(self) -> None:
        return None


class ClamAVScanner(MalwareScannerPort):
    def __init__(self, host: str, port: int, timeout_seconds: float) -> None:
        self._host = host
        self._port = port
        self._timeout = timeout_seconds

    async def scan(self, path: Path) -> ScanResult:
        try:
            reply = await asyncio.wait_for(
                self._exchange(lambda writer: _send_file(writer, path)), timeout=self._timeout
            )
        except (OSError, TimeoutError, asyncio.IncompleteReadError) as exc:
            raise ScannerUnavailableError(str(exc)) from exc
        return _parse_verdict(reply)

    async def self_test(self) -> None:
        """Raise ScannerUnavailableError unless clamd scans a harmless sample as clean."""
        try:
            reply = await asyncio.wait_for(
                self._exchange(lambda writer: _send_bytes(writer, _SELF_TEST_PAYLOAD)),
                timeout=self._timeout,
            )
        except (OSError, TimeoutError, asyncio.IncompleteReadError) as exc:
            raise ScannerUnavailableError(str(exc)) from exc
        # `_parse_verdict` rejects ERROR replies; a FOUND on the harmless sample is also a fault.
        if not _parse_verdict(reply).clean:
            raise ScannerUnavailableError(f"clamd self-test flagged the sample: {reply!r}")

    async def _exchange(self, send: Sender) -> str:
        reader, writer = await asyncio.open_connection(self._host, self._port)
        try:
            writer.write(b"zINSTREAM\0")
            await send(writer)
            writer.write(struct.pack(">I", 0))  # zero-length chunk ends the stream
            await writer.drain()
            # clamd ends every reply with NUL. readuntil() caps the buffer (asyncio's default of
            # 64 KiB), so a misbehaving server cannot grow memory without limit.
            try:
                raw = await reader.readuntil(b"\0")
            except (asyncio.LimitOverrunError, asyncio.IncompleteReadError) as exc:
                raise ScannerUnavailableError(
                    "clamd reply was not a bounded NUL-terminated line"
                ) from exc
            return raw.rstrip(b"\0").decode("utf-8", errors="replace")
        finally:
            writer.close()
            await writer.wait_closed()


async def _send_file(writer: asyncio.StreamWriter, path: Path) -> None:
    handle = await asyncio.to_thread(path.open, "rb")
    try:
        while block := await asyncio.to_thread(handle.read, _READ_BLOCK):
            writer.write(struct.pack(">I", len(block)) + block)
            await writer.drain()
    finally:
        await asyncio.to_thread(handle.close)


async def _send_bytes(writer: asyncio.StreamWriter, payload: bytes) -> None:
    writer.write(struct.pack(">I", len(payload)) + payload)
    await writer.drain()


def _parse_verdict(reply: str) -> ScanResult:
    _, _, verdict = reply.partition(": ")
    if verdict == "OK":
        return ScanResult(clean=True)
    if verdict.endswith(" FOUND"):
        return ScanResult(clean=False, signature=verdict.removesuffix(" FOUND"))
    raise ScannerUnavailableError(f"unexpected clamd reply: {reply!r}")
