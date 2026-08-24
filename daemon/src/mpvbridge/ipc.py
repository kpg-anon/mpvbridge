"""Asyncio client for mpv's JSON IPC socket (``--input-ipc-server``)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)

EventHandler = Callable[[dict[str, Any]], Awaitable[None]]

#: asyncio's StreamReader defaults to a 64 KiB line limit, and mpv sends the whole ``playlist``
#: property as a single line -- roughly 200 KB for an 855-entry YouTube playlist. Past the limit
#: readline() raises instead of returning, which killed the reader task and silently froze every
#: property update while commands carried on working.
STREAM_LIMIT_BYTES = 16 * 1024 * 1024


class MpvIpcError(RuntimeError):
    """mpv answered a command with ``error`` set to something other than ``success``."""


class MpvIpc:
    """Speaks mpv's line-delimited JSON IPC protocol over a unix socket.

    mpv replies to every command with the ``request_id`` it was given, so requests are matched to
    responses through a table of futures. Everything without a ``request_id`` is an event and goes
    to the handler passed to :meth:`start`.
    """

    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._next_request_id = 1
        self._observe_ids: dict[int, str] = {}
        self._next_observe_id = 1
        self._reader_task: asyncio.Task[None] | None = None
        self._on_event: EventHandler | None = None
        self._write_lock = asyncio.Lock()
        self.closed = asyncio.Event()

    async def connect(self, timeout: float = 15.0, poll_interval: float = 0.1) -> None:
        """Wait for mpv to create the socket, then connect to it."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        last_error: OSError | None = None
        while loop.time() < deadline:
            try:
                self._reader, self._writer = await asyncio.open_unix_connection(
                    self.socket_path, limit=STREAM_LIMIT_BYTES
                )
            except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
                last_error = exc
                await asyncio.sleep(poll_interval)
            else:
                log.debug("connected to mpv ipc at %s", self.socket_path)
                return
        raise TimeoutError(f"mpv IPC socket {self.socket_path} did not appear") from last_error

    def start(self, on_event: EventHandler) -> None:
        self._on_event = on_event
        self._reader_task = asyncio.create_task(self._read_loop(), name="mpv-ipc-reader")

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("ignoring malformed line from mpv: %r", line[:200])
                    continue
                await self._dispatch(message)
        except (ConnectionResetError, BrokenPipeError):
            pass
        except ValueError:
            # A line longer than STREAM_LIMIT_BYTES. Never silently: losing the reader means
            # losing every property update while writes keep appearing to work.
            log.error(
                "mpv sent a line larger than %d bytes; IPC reader stopping", STREAM_LIMIT_BYTES
            )
        finally:
            self._fail_pending(ConnectionResetError("mpv IPC closed"))
            self.closed.set()

    async def _dispatch(self, message: dict[str, Any]) -> None:
        request_id = message.get("request_id")
        if request_id is not None:
            future = self._pending.pop(request_id, None)
            if future is None or future.done():
                return
            if message.get("error", "success") != "success":
                future.set_exception(MpvIpcError(message.get("error", "unknown error")))
            else:
                future.set_result(message.get("data"))
            return

        if "event" in message and self._on_event is not None:
            # Attach the property name we registered the observe id under, because mpv only
            # guarantees the id round-trips.
            if message.get("event") == "property-change" and "name" not in message:
                observed = self._observe_ids.get(message.get("id", -1))
                if observed is not None:
                    message["name"] = observed
            await self._on_event(message)

    def _fail_pending(self, exc: BaseException) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

    async def command(self, *args: Any) -> Any:
        """Run an mpv command and return its ``data``."""
        if self._writer is None:
            raise ConnectionError("not connected to mpv")
        if self._reader_task is None:
            # Every command awaits a reply that only the reader task can deliver, so issuing one
            # first would deadlock rather than fail.
            raise ConnectionError("MpvIpc.start() must be called before any command")
        request_id = self._next_request_id
        self._next_request_id += 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        payload = json.dumps({"command": list(args), "request_id": request_id}) + "\n"
        async with self._write_lock:
            self._writer.write(payload.encode())
            await self._writer.drain()
        return await future

    async def get_property(self, name: str) -> Any:
        return await self.command("get_property", name)

    async def set_property(self, name: str, value: Any) -> Any:
        return await self.command("set_property", name, value)

    async def observe_property(self, name: str) -> int:
        observe_id = self._next_observe_id
        self._next_observe_id += 1
        self._observe_ids[observe_id] = name
        await self.command("observe_property", observe_id, name)
        return observe_id

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()
            self._writer = None
        self._reader = None
        self.closed.set()
