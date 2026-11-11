from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable


class HealthServer:
    """Minimal dependency-free HTTP readiness server for the worker process."""

    def __init__(
        self,
        port: int,
        readiness: Callable[[], Awaitable[None]],
    ) -> None:
        self._port = port
        self._readiness = readiness
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "0.0.0.0", self._port)

    async def aclose(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=1.0)
            path = request_line.decode(errors="replace").split(" ")[1]
            if path == "/healthz":
                status, body = "200 OK", b"ok"
            elif path == "/readyz":
                try:
                    await asyncio.wait_for(self._readiness(), timeout=2.0)
                    status, body = "200 OK", b"ready"
                except Exception:  # noqa: BLE001 - readiness reports, never crashes
                    status, body = "503 Service Unavailable", b"not ready"
            else:
                status, body = "404 Not Found", b"not found"
            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Length: {len(body)}\r\n"
                "Content-Type: text/plain\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
