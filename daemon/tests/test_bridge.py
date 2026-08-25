"""Command mapping: what the app asks for vs what mpv is actually told."""

from __future__ import annotations

import asyncio

from mpvbridge.bridge import Bridge
from mpvbridge.cache import PlaylistCache
from mpvbridge.ipc import MpvIpcError


class FakeIpc:
    def __init__(self, fail: bool = False):
        self.calls: list[tuple] = []
        self.fail = fail

    async def command(self, *args):
        if self.fail:
            raise MpvIpcError("boom")
        self.calls.append(("command", *args))

    async def set_property(self, name, value):
        if self.fail:
            raise MpvIpcError("boom")
        self.calls.append(("set_property", name, value))

    async def get_property(self, name):
        return None


class FakeServer:
    def __init__(self):
        self.broadcasts: list[dict] = []

    async def broadcast(self, message):
        self.broadcasts.append(message)


def dispatch(name: str, message: dict | None = None, fail: bool = False) -> FakeIpc:
    ipc = FakeIpc(fail=fail)
    bridge = Bridge(ipc, FakeServer())
    asyncio.run(bridge.handle_command(name, message or {"type": "cmd", "name": name}))
    return ipc


def test_next_and_previous_use_weak_playlist_commands():
    assert dispatch("next").calls == [("command", "playlist-next", "weak")]
    assert dispatch("previous").calls == [("command", "playlist-prev", "weak")]


def test_play_and_pause_set_the_pause_property():
    assert dispatch("play").calls == [("set_property", "pause", False)]
    assert dispatch("pause").calls == [("set_property", "pause", True)]


def test_toggle_cycles_pause():
    assert dispatch("toggle").calls == [("command", "cycle", "pause")]


def test_stop_pauses_rather_than_unloading_the_playlist():
    assert dispatch("stop").calls == [("set_property", "pause", True)]


def test_seek_is_absolute():
    calls = dispatch("seek", {"type": "cmd", "name": "seek", "position": 42.5}).calls
    assert calls == [("command", "seek", 42.5, "absolute")]


def test_seek_without_a_position_does_nothing():
    assert dispatch("seek", {"type": "cmd", "name": "seek"}).calls == []


def test_goto_sets_playlist_pos():
    calls = dispatch("goto", {"type": "cmd", "name": "goto", "index": 11}).calls
    assert calls == [("set_property", "playlist-pos", 11)]


def test_goto_rejects_a_negative_index():
    assert dispatch("goto", {"type": "cmd", "name": "goto", "index": -1}).calls == []


def test_refresh_touches_mpv_not_at_all():
    assert dispatch("refresh").calls == []


def test_a_failing_command_is_swallowed_not_raised():
    # mpv dying mid-command must not take the bridge down with it.
    assert dispatch("next", fail=True).calls == []


def test_snapshot_messages_are_ordered_hello_then_state(tmp_path):
    bridge = Bridge(FakeIpc(), FakeServer(), cache=PlaylistCache(tmp_path))
    bridge.mpv_version = "0.38.0"
    messages = bridge.snapshot_messages()

    assert [message["type"] for message in messages] == ["hello", "state", "library"]
    assert messages[0]["mpv"] == "0.38.0"


def test_snapshot_includes_playlist_once_known(tmp_path):
    bridge = Bridge(FakeIpc(), FakeServer(), cache=PlaylistCache(tmp_path))
    bridge._playlist_message = {"type": "playlist", "entries": []}  # noqa: SLF001

    assert [message["type"] for message in bridge.snapshot_messages()] == [
        "hello",
        "state",
        "playlist",
        "library",
    ]


def test_shuffle_reorders_the_playlist_in_place():
    assert dispatch("shuffle").calls == [("command", "playlist-shuffle")]


def test_unshuffle_restores_order():
    assert dispatch("unshuffle").calls == [("command", "playlist-unshuffle")]
