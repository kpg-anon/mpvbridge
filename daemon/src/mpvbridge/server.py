"""Loopback TCP server the Android companion app connects to."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from .protocol import (
    CLIENT_COMMAND,
    CLIENT_HELLO,
    COMMAND_NAMES,
    DEFAULT_HOST,
    DEFAULT_PORT,
    error_message,
)

log = logging.getLogger(__name__)

CommandHandler = Callable[[str, dict[str, Any]], Awaitable[None]]
SnapshotProvider = Callable[[], list[dict[str, Any]]]

MAX_LINE_BYTES = 1 << 20


class BridgeServer:
    """Broadcasts state to every connected client and forwards their commands to mpv.

    The server binds loopback only, so it is reachable from other apps on the device but not from
    the network. ``token`` adds a shared secret to the handshake for people who want it.
    """

    def __init__(
        self,
        on_command: CommandHandler,
        snapshot_provider: SnapshotProvider,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        token: str | None = None,
    ) -> None:
        self._on_command = on_command
        self._snapshot_provider = snapshot_provider
        self._host = host
        self._port = port
        self._token = token
        self._server: asyncio.Server | None = None
        self._clients: set[asyncio.StreamWriter] = set()

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_client, self._host, self._port)
        log.info("bridge listening on %s:%d", self._host, self._port)

    async def stop(self) -> None:
        for writer in list(self._clients):
            await self._disconnect(writer)
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    async def broadcast(self, message: dict[str, Any]) -> None:
        if not self._clients:
            return
        payload = (json.dumps(message, separators=(",", ":")) + "\n").encode()
        for writer in list(self._clients):
            await self._send_raw(writer, payload)

    async def _send(self, writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
        await self._send_raw(writer, (json.dumps(message, separators=(",", ":")) + "\n").encode())

    async def _send_raw(self, writer: asyncio.StreamWriter, payload: bytes) -> None:
        try:
            writer.write(payload)
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError, OSError):
            await self._disconnect(writer)

    async def _disconnect(self, writer: asyncio.StreamWriter) -> None:
        self._clients.discard(writer)
        with contextlib.suppress(Exception):
            writer.close()
            await writer.wait_closed()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        log.info("client connected: %s", peer)
        authenticated = self._token is None
        greeted = False
        self._clients.add(writer)
        try:
            if authenticated:
                await self._greet(writer)
                greeted = True
            while True:
                try:
                    line = await reader.readline()
                except (ConnectionResetError, OSError):
                    break
                if not line:
                    break
                if len(line) > MAX_LINE_BYTES:
                    await self._send(writer, error_message("line too long"))
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    await self._send(writer, error_message("malformed json"))
                    continue
                if not isinstance(message, dict):
                    await self._send(writer, error_message("expected a json object"))
                    continue

                kind = message.get("type")
                if kind == CLIENT_HELLO:
                    if not authenticated:
                        if message.get("token") != self._token:
                            await self._send(writer, error_message("bad token"))
                            break
                        authenticated = True
                    if not greeted:
                        await self._greet(writer)
                        greeted = True
                    continue

                if not authenticated:
                    await self._send(writer, error_message("hello first"))
                    continue

                if kind == CLIENT_COMMAND:
                    name = message.get("name")
                    if name not in COMMAND_NAMES:
                        await self._send(writer, error_message(f"unknown command: {name!r}"))
                        continue
                    await self._on_command(name, message)
                    continue

                await self._send(writer, error_message(f"unknown message type: {kind!r}"))
        finally:
            log.info("client disconnected: %s", peer)
            await self._disconnect(writer)

    async def _greet(self, writer: asyncio.StreamWriter) -> None:
        for message in self._snapshot_provider():
            await self._send(writer, message)
