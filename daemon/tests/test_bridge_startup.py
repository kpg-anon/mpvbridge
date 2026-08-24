"""Startup ordering and hang-resistance.

These cover a bug that made the whole bridge look dead on the phone: the version query was issued
before the IPC reader task existed, so it awaited a reply nothing could ever deliver. mpv kept
playing, the socket kept listening, and every command silently hung.
"""

from __future__ import annotations

import asyncio

import pytest
from mpvbridge import bridge as bridge_module
from mpvbridge.bridge import Bridge
from mpvbridge.state import OBSERVED_PROPERTIES


class OrderTrackingIpc:
    """Refuses commands before start(), exactly as the real MpvIpc now does."""

    def __init__(self) -> None:
        self.started = False
        self.calls: list[tuple] = []

    def start(self, on_event) -> None:
        self.started = True

    async def command(self, *args):
        if not self.started:
            raise AssertionError("command issued before the IPC reader was started")
        self.calls.append(args)
        return None

    async def get_property(self, name):
        return await self.command("get_property", name)

    async def observe_property(self, name):
        return await self.command("observe_property", name)


class HangingIpc(OrderTrackingIpc):
    async def command(self, *args):
        await asyncio.Event().wait()  # never resolves


class FakeServer:
    def __init__(self) -> None:
        self.broadcasts: list[dict] = []

    async def broadcast(self, message) -> None:
        self.broadcasts.append(message)


def run(coro):
    return asyncio.run(coro)


def test_reader_is_started_before_anything_is_asked_of_mpv():
    ipc = OrderTrackingIpc()
    bridge = Bridge(ipc, FakeServer())

    async def scenario():
        await bridge.start()
        await bridge.stop()

    run(scenario())

    assert ipc.started
    assert ("get_property", "mpv-version") in ipc.calls


def test_every_observed_property_is_registered():
    ipc = OrderTrackingIpc()
    bridge = Bridge(ipc, FakeServer())

    async def scenario():
        await bridge.start()
        await bridge.stop()

    run(scenario())

    observed = {args[1] for args in ipc.calls if args[0] == "observe_property"}
    assert observed == set(OBSERVED_PROPERTIES)


def test_a_hanging_mpv_does_not_wedge_startup(monkeypatch):
    monkeypatch.setattr(bridge_module, "IPC_TIMEOUT_SECONDS", 0.05)
    bridge = Bridge(HangingIpc(), FakeServer())

    async def scenario():
        await asyncio.wait_for(bridge.start(), timeout=5)
        await bridge.stop()

    run(scenario())  # must not raise TimeoutError


def test_a_hanging_mpv_does_not_wedge_a_command(monkeypatch):
    monkeypatch.setattr(bridge_module, "IPC_TIMEOUT_SECONDS", 0.05)
    bridge = Bridge(HangingIpc(), FakeServer())

    async def scenario():
        await asyncio.wait_for(
            bridge.handle_command("next", {"type": "cmd", "name": "next"}), timeout=5
        )

    run(scenario())


def test_command_before_start_raises_instead_of_hanging():
    from mpvbridge.ipc import MpvIpc

    ipc = MpvIpc("/nonexistent")
    ipc._writer = object()  # noqa: SLF001 - pretend we connected

    async def scenario():
        with pytest.raises(ConnectionError, match="start"):
            await ipc.command("get_property", "pause")

    run(scenario())
