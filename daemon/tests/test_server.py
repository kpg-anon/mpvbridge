"""Exercises the TCP server over a real loopback socket."""

from __future__ import annotations

import asyncio
import json

from mpvbridge.server import BridgeServer


class Harness:
    def __init__(self, token: str | None = None):
        self.commands: list[tuple[str, dict]] = []
        self.token = token
        self.server: BridgeServer | None = None
        self.port = 0

    async def __aenter__(self):
        async def on_command(name: str, message: dict) -> None:
            self.commands.append((name, message))

        def snapshot():
            return [{"type": "hello", "version": 1, "mpv": "0.38.0"}]

        self.server = BridgeServer(
            on_command=on_command,
            snapshot_provider=snapshot,
            host="127.0.0.1",
            port=0,
            token=self.token,
        )
        await self.server.start()
        self.port = self.server._server.sockets[0].getsockname()[1]  # noqa: SLF001
        return self

    async def __aexit__(self, *exc):
        assert self.server is not None
        await self.server.stop()

    async def connect(self):
        return await asyncio.open_connection("127.0.0.1", self.port)


async def read_message(reader: asyncio.StreamReader) -> dict:
    line = await asyncio.wait_for(reader.readline(), timeout=2)
    return json.loads(line)


async def send(writer: asyncio.StreamWriter, message: dict) -> None:
    writer.write((json.dumps(message) + "\n").encode())
    await writer.drain()


def test_client_is_greeted_with_a_snapshot():
    async def scenario():
        async with Harness() as harness:
            reader, writer = await harness.connect()
            greeting = await read_message(reader)
            writer.close()
            return greeting

    greeting = asyncio.run(scenario())
    assert greeting["type"] == "hello"
    assert greeting["mpv"] == "0.38.0"


def test_command_reaches_the_handler():
    async def scenario():
        async with Harness() as harness:
            reader, writer = await harness.connect()
            await read_message(reader)
            await send(writer, {"type": "cmd", "name": "next"})
            await asyncio.sleep(0.1)
            writer.close()
            return harness.commands

    commands = asyncio.run(scenario())
    assert commands == [("next", {"type": "cmd", "name": "next"})]


def test_unknown_command_is_rejected_without_dropping_the_client():
    async def scenario():
        async with Harness() as harness:
            reader, writer = await harness.connect()
            await read_message(reader)
            await send(writer, {"type": "cmd", "name": "self-destruct"})
            error = await read_message(reader)
            await send(writer, {"type": "cmd", "name": "previous"})
            await asyncio.sleep(0.1)
            writer.close()
            return error, harness.commands

    error, commands = asyncio.run(scenario())
    assert error["type"] == "error"
    assert "self-destruct" in error["reason"]
    assert [name for name, _ in commands] == ["previous"]


def test_malformed_json_is_reported_and_survivable():
    async def scenario():
        async with Harness() as harness:
            reader, writer = await harness.connect()
            await read_message(reader)
            writer.write(b"{not json at all\n")
            await writer.drain()
            error = await read_message(reader)
            await send(writer, {"type": "cmd", "name": "toggle"})
            await asyncio.sleep(0.1)
            writer.close()
            return error, harness.commands

    error, commands = asyncio.run(scenario())
    assert error["reason"] == "malformed json"
    assert [name for name, _ in commands] == ["toggle"]


def test_batched_and_split_lines_are_framed_correctly():
    async def scenario():
        async with Harness() as harness:
            reader, writer = await harness.connect()
            await read_message(reader)
            # Two messages in one write, then a third split across two writes.
            writer.write(b'{"type":"cmd","name":"next"}\n{"type":"cmd","name":"next"}\n')
            writer.write(b'{"type":"cmd","na')
            await writer.drain()
            await asyncio.sleep(0.05)
            writer.write(b'me":"previous"}\n')
            await writer.drain()
            await asyncio.sleep(0.1)
            writer.close()
            return harness.commands

    commands = asyncio.run(scenario())
    assert [name for name, _ in commands] == ["next", "next", "previous"]


def test_token_gates_commands():
    async def scenario():
        async with Harness(token="s3cret") as harness:
            reader, writer = await harness.connect()
            # No greeting until the handshake succeeds.
            await send(writer, {"type": "cmd", "name": "next"})
            refusal = await read_message(reader)

            await send(writer, {"type": "hello", "token": "s3cret"})
            greeting = await read_message(reader)
            await send(writer, {"type": "cmd", "name": "next"})
            await asyncio.sleep(0.1)
            writer.close()
            return refusal, greeting, harness.commands

    refusal, greeting, commands = asyncio.run(scenario())
    assert refusal["reason"] == "hello first"
    assert greeting["type"] == "hello"
    assert [name for name, _ in commands] == ["next"]


def test_bad_token_is_refused():
    async def scenario():
        async with Harness(token="s3cret") as harness:
            reader, writer = await harness.connect()
            await send(writer, {"type": "hello", "token": "wrong"})
            refusal = await read_message(reader)
            writer.close()
            return refusal

    refusal = asyncio.run(scenario())
    assert refusal["reason"] == "bad token"


def test_broadcast_reaches_every_client():
    async def scenario():
        async with Harness() as harness:
            reader_a, writer_a = await harness.connect()
            reader_b, writer_b = await harness.connect()
            await read_message(reader_a)
            await read_message(reader_b)

            assert harness.server is not None
            await harness.server.broadcast({"type": "state", "title": "Song"})

            first = await read_message(reader_a)
            second = await read_message(reader_b)
            writer_a.close()
            writer_b.close()
            return first, second

    first, second = asyncio.run(scenario())
    assert first["title"] == "Song"
    assert second["title"] == "Song"
