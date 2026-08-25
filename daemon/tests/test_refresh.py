"""play-url and the playlist refresh."""

from __future__ import annotations

import asyncio

from mpvbridge.bridge import Bridge
from mpvbridge.cache import PlaylistCache
from mpvbridge.ipc import MpvIpcError

SOURCE = "https://www.youtube.com/playlist?list=PLFx"


class FakeIpc:
    def __init__(self, playlist=None, reject_insert_next=False):
        self.calls: list[tuple] = []
        self.playlist = playlist or []
        self.reject_insert_next = reject_insert_next
        self.started = False

    def start(self, on_event):
        self.started = True

    async def command(self, *args):
        if self.reject_insert_next and args[:1] == ("loadfile",) and "insert-next-play" in args:
            raise MpvIpcError("unknown flag")
        self.calls.append(args)
        return None

    async def get_property(self, name):
        if name == "playlist":
            return self.playlist
        return None

    async def set_property(self, name, value):
        self.calls.append(("set_property", name, value))


class FakeServer:
    def __init__(self):
        self.broadcasts: list[dict] = []

    async def broadcast(self, message):
        self.broadcasts.append(message)


def play_url(ipc, url=SOURCE):
    bridge = Bridge(ipc, FakeServer())
    asyncio.run(bridge.handle_command("play-url", {"type": "cmd", "name": "play-url", "url": url}))
    return ipc.calls


def test_play_url_inserts_after_the_current_entry():
    """`replace` would discard an 855-entry playlist, so it must not be used."""
    calls = play_url(FakeIpc())

    assert calls == [("loadfile", SOURCE, "insert-next-play")]
    assert not any("replace" in call for call in calls)


def test_play_url_falls_back_when_insert_next_play_is_unsupported():
    calls = play_url(FakeIpc(reject_insert_next=True))

    assert calls == [("loadfile", SOURCE, "append-play")]


def test_play_url_without_a_url_does_nothing():
    ipc = FakeIpc()
    bridge = Bridge(ipc, FakeServer())
    asyncio.run(bridge.handle_command("play-url", {"type": "cmd", "name": "play-url"}))

    assert ipc.calls == []


def run_refresh(monkeypatch, tmp_path, fetched, playlist, source=SOURCE):
    ipc = FakeIpc(playlist=playlist)
    server = FakeServer()
    bridge = Bridge(ipc, server, source_url=source, cache=PlaylistCache(tmp_path))

    async def fake_fetch(url):
        return "Sample playlist", fetched

    monkeypatch.setattr(bridge, "_fetch_playlist", fake_fetch)
    monkeypatch.setattr("mpvbridge.bridge.shutil.which", lambda _: "/usr/bin/yt-dlp")
    asyncio.run(bridge._refresh_playlist())  # noqa: SLF001
    return ipc, server, bridge


def test_refresh_appends_only_entries_mpv_does_not_have(monkeypatch, tmp_path):
    playlist = [
        {"filename": "https://youtu.be/aaaaaaaaaaa", "title": "Have it"},
        {"filename": "https://youtu.be/bbbbbbbbbbb", "title": "Have it too"},
    ]
    fetched = [
        ("aaaaaaaaaaa", "Have it"),
        ("bbbbbbbbbbb", "Have it too"),
        ("ccccccccccc", "Brand new"),
    ]

    ipc, server, _ = run_refresh(monkeypatch, tmp_path, fetched, playlist)

    appends = [call for call in ipc.calls if call[0] == "loadfile"]
    assert appends == [("loadfile", "https://www.youtube.com/watch?v=ccccccccccc", "append")]

    done = [m for m in server.broadcasts if m.get("status") == "done"][0]
    assert done["added"] == 1
    assert done["total"] == 3


def test_refresh_reports_up_to_date(monkeypatch, tmp_path):
    playlist = [{"filename": "https://youtu.be/aaaaaaaaaaa", "title": "Have it"}]
    fetched = [("aaaaaaaaaaa", "Have it")]

    ipc, server, _ = run_refresh(monkeypatch, tmp_path, fetched, playlist)

    assert [call for call in ipc.calls if call[0] == "loadfile"] == []
    assert [m for m in server.broadcasts if m.get("status") == "done"][0]["added"] == 0


def test_refresh_announces_that_it_started(monkeypatch, tmp_path):
    _, server, _ = run_refresh(monkeypatch, tmp_path, [("aaaaaaaaaaa", "x")], [])

    assert server.broadcasts[0]["type"] == "refresh"
    assert server.broadcasts[0]["status"] == "running"


def test_refresh_populates_the_cache(monkeypatch, tmp_path):
    fetched = [("aaaaaaaaaaa", "First"), ("bbbbbbbbbbb", "Second")]

    _, _, bridge = run_refresh(monkeypatch, tmp_path, fetched, [])

    assert bridge.cache.load_playlist(SOURCE) == ["aaaaaaaaaaa", "bbbbbbbbbbb"]
    assert bridge.cache.title_for("bbbbbbbbbbb") == "Second"


def test_refresh_without_a_source_url_reports_an_error(monkeypatch, tmp_path):
    ipc = FakeIpc()
    server = FakeServer()
    bridge = Bridge(ipc, server, source_url=None, cache=PlaylistCache(tmp_path))
    asyncio.run(bridge._refresh_playlist())  # noqa: SLF001

    assert server.broadcasts[-1]["status"] == "error"
    assert "source" in server.broadcasts[-1]["reason"]


def test_refresh_without_yt_dlp_reports_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr("mpvbridge.bridge.shutil.which", lambda _: None)
    server = FakeServer()
    bridge = Bridge(FakeIpc(), server, source_url=SOURCE, cache=PlaylistCache(tmp_path))
    asyncio.run(bridge._refresh_playlist())  # noqa: SLF001

    assert server.broadcasts[-1]["status"] == "error"
    assert "yt-dlp" in server.broadcasts[-1]["reason"]


def test_cached_playlist_is_published_before_mpv_expands_it(tmp_path):
    """The responsiveness win: a warm cache means a full list on connect."""
    cache = PlaylistCache(tmp_path)
    cache.save_playlist(SOURCE, ["aaaaaaaaaaa", "bbbbbbbbbbb"])
    cache.remember_titles({"aaaaaaaaaaa": "First", "bbbbbbbbbbb": "Second"})

    bridge = Bridge(FakeIpc(), FakeServer(), source_url=SOURCE, cache=cache)
    bridge._seed_playlist_from_cache()  # noqa: SLF001

    messages = bridge.snapshot_messages()
    playlist = [m for m in messages if m["type"] == "playlist"][0]

    assert [e["title"] for e in playlist["entries"]] == ["First", "Second"]
    assert playlist["entries"][0]["url"] == "https://www.youtube.com/watch?v=aaaaaaaaaaa"


def test_seeding_is_skipped_without_a_cache_entry(tmp_path):
    bridge = Bridge(FakeIpc(), FakeServer(), source_url=SOURCE, cache=PlaylistCache(tmp_path))
    bridge._seed_playlist_from_cache()  # noqa: SLF001

    assert [m["type"] for m in bridge.snapshot_messages()] == ["hello", "state", "library"]
