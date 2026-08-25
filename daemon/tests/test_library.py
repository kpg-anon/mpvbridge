"""The playlist library: naming a playlist, remembering it, and switching mpv to it.

This is what lets the app be the way in. mpv is started idle with no arguments and told what to
play over the socket, rather than the playlist being fixed on the Termux command line.
"""

from __future__ import annotations

import asyncio
import json

from mpvbridge.__main__ import wants_idle
from mpvbridge.bridge import Bridge
from mpvbridge.cache import PlaylistCache
from mpvbridge.ipc import MpvIpcError
from mpvbridge.state import build_playlist

SOURCE = "https://www.youtube.com/playlist?list=PLshare"
OTHER = "https://www.youtube.com/playlist?list=PLother"


class FakeIpc:
    def __init__(self, playlist=None):
        self.calls: list[tuple] = []
        self.playlist = playlist or []

    def start(self, on_event):
        pass

    async def command(self, *args):
        self.calls.append(args)
        return None

    async def get_property(self, name):
        return self.playlist if name == "playlist" else None

    async def set_property(self, name, value):
        self.calls.append(("set_property", name, value))


class FakeServer:
    def __init__(self):
        self.broadcasts: list[dict] = []

    async def broadcast(self, message):
        self.broadcasts.append(message)


def make_bridge(tmp_path, source_url=None, playlist=None):
    ipc = FakeIpc(playlist=playlist)
    server = FakeServer()
    bridge = Bridge(ipc, server, source_url=source_url, cache=PlaylistCache(tmp_path))
    return ipc, server, bridge


def library_of(server):
    return [m for m in server.broadcasts if m["type"] == "library"][-1]["playlists"]


# -- cache -------------------------------------------------------------------------------------


def test_known_playlists_reports_title_and_count(tmp_path):
    cache = PlaylistCache(tmp_path)
    cache.save_playlist(SOURCE, ["aaaaaaaaaaa", "bbbbbbbbbbb"], title="share")

    (entry,) = cache.known_playlists()
    assert entry["url"] == SOURCE
    assert entry["title"] == "share"
    assert entry["count"] == 2
    assert entry["fetched"] > 0
    assert cache.playlist_title(SOURCE) == "share"


def test_a_save_without_a_title_keeps_the_one_we_already_had(tmp_path):
    """mpv's own playlist updates carry no playlist title; they must not erase yt-dlp's."""
    cache = PlaylistCache(tmp_path)
    cache.save_playlist(SOURCE, ["aaaaaaaaaaa"], title="share")
    cache.save_playlist(SOURCE, ["aaaaaaaaaaa", "bbbbbbbbbbb"])

    assert cache.playlist_title(SOURCE) == "share"
    assert cache.load_playlist(SOURCE) == ["aaaaaaaaaaa", "bbbbbbbbbbb"]


def test_forget_playlist_removes_the_ids_and_the_playlist_file(tmp_path):
    cache = PlaylistCache(tmp_path)
    cache.save_playlist(SOURCE, ["aaaaaaaaaaa"], title="share")
    cache.write_m3u(SOURCE, ["aaaaaaaaaaa"])

    assert cache.forget_playlist(SOURCE) is True
    assert cache.known_playlists() == []
    assert not cache.m3u_path(SOURCE).exists()


def test_known_playlists_puts_the_most_recently_fetched_first(tmp_path):
    cache = PlaylistCache(tmp_path)
    cache.save_playlist(SOURCE, ["aaaaaaaaaaa"], title="older")
    cache.save_playlist(OTHER, ["bbbbbbbbbbb"], title="newer")
    # save_playlist stamps whole seconds, so two saves inside one test tie. Age one by hand.
    path = cache.playlist_path(SOURCE)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["fetched"] -= 3600
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert [entry["title"] for entry in cache.known_playlists()] == ["newer", "older"]


# -- adding ------------------------------------------------------------------------------------


def test_adding_a_playlist_names_it_from_yt_dlp(monkeypatch, tmp_path):
    _, server, bridge = make_bridge(tmp_path)

    async def fake_fetch(url):
        return "share", [("aaaaaaaaaaa", "First"), ("bbbbbbbbbbb", "Second")]

    monkeypatch.setattr(bridge, "_fetch_playlist", fake_fetch)
    monkeypatch.setattr("mpvbridge.bridge.shutil.which", lambda _: "/usr/bin/yt-dlp")
    asyncio.run(bridge._add_playlist(SOURCE))  # noqa: SLF001

    assert library_of(server) == [
        {"url": SOURCE, "title": "share", "count": 2, "fetched": library_of(server)[0]["fetched"],
         "current": False}
    ]
    assert bridge.cache.title_for("aaaaaaaaaaa") == "First"


def test_adding_reports_progress_before_yt_dlp_finishes(monkeypatch, tmp_path):
    """yt-dlp on a large playlist takes many seconds; the app must not look frozen."""
    _, server, bridge = make_bridge(tmp_path)

    async def fake_fetch(url):
        return "share", [("aaaaaaaaaaa", "First")]

    monkeypatch.setattr(bridge, "_fetch_playlist", fake_fetch)
    monkeypatch.setattr("mpvbridge.bridge.shutil.which", lambda _: "/usr/bin/yt-dlp")
    asyncio.run(bridge._add_playlist(SOURCE))  # noqa: SLF001

    assert server.broadcasts[0]["type"] == "refresh"
    assert server.broadcasts[0]["status"] == "running"
    assert server.broadcasts[0]["url"] == SOURCE

    done = [m for m in server.broadcasts if m.get("status") == "done"][0]
    assert done["title"] == "share"
    assert done["total"] == 1


def test_adding_without_yt_dlp_reports_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr("mpvbridge.bridge.shutil.which", lambda _: None)
    _, server, bridge = make_bridge(tmp_path)
    asyncio.run(bridge._add_playlist(SOURCE))  # noqa: SLF001

    assert server.broadcasts[-1]["status"] == "error"
    assert "yt-dlp" in server.broadcasts[-1]["reason"]
    assert server.broadcasts[-1]["url"] == SOURCE


def test_adding_and_rechecking_are_told_apart(monkeypatch, tmp_path):
    """"Added 855 tracks" and "found 855 tracks" are not the same sentence."""
    _, server, bridge = make_bridge(tmp_path, source_url=SOURCE)

    async def fake_fetch(url):
        return "share", [("aaaaaaaaaaa", "First")]

    monkeypatch.setattr(bridge, "_fetch_playlist", fake_fetch)
    monkeypatch.setattr("mpvbridge.bridge.shutil.which", lambda _: "/usr/bin/yt-dlp")

    asyncio.run(bridge._add_playlist(OTHER))  # noqa: SLF001
    asyncio.run(bridge._refresh_playlist())  # noqa: SLF001

    kinds = [(m["kind"], m["status"]) for m in server.broadcasts if m["type"] == "refresh"]
    assert ("add", "running") in kinds
    assert ("add", "done") in kinds
    assert ("refresh", "running") in kinds
    assert ("refresh", "done") in kinds


def test_a_placeholder_title_is_never_cached(monkeypatch, tmp_path):
    """Caching "[Deleted video]" would make a dead entry look resolved, and Hide unavailable
    tracks would quietly stop hiding it."""
    _, _, bridge = make_bridge(tmp_path)

    async def fake_fetch(url):
        return "share", [("aaaaaaaaaaa", "Real song"), ("bbbbbbbbbbb", "[Deleted video]")]

    monkeypatch.setattr(bridge, "_fetch_playlist", fake_fetch)
    monkeypatch.setattr("mpvbridge.bridge.shutil.which", lambda _: "/usr/bin/yt-dlp")
    asyncio.run(bridge._add_playlist(SOURCE))  # noqa: SLF001

    assert bridge.cache.title_for("aaaaaaaaaaa") == "Real song"
    assert bridge.cache.title_for("bbbbbbbbbbb") is None
    # The id is still in the playlist -- it is only its title we refuse to trust.
    assert bridge.cache.load_playlist(SOURCE) == ["aaaaaaaaaaa", "bbbbbbbbbbb"]


def test_a_cache_already_holding_a_placeholder_keeps_the_entry_flagged(tmp_path):
    _, _, bridge = make_bridge(tmp_path, source_url=SOURCE)
    bridge.cache.remember_titles({"bbbbbbbbbbb": "[Deleted video]"})

    playlist = build_playlist(
        [{"filename": "https://www.youtube.com/watch?v=bbbbbbbbbbb"}]
    )
    bridge._enrich(playlist)  # noqa: SLF001

    assert playlist.entries[0].unavailable is True


# -- loading -----------------------------------------------------------------------------------


def test_loading_a_cached_playlist_never_touches_yt_dlp(monkeypatch, tmp_path):
    """The whole point of the cache: picking a playlist in the app plays it straight away."""
    ipc, server, bridge = make_bridge(tmp_path)
    bridge.cache.save_playlist(SOURCE, ["aaaaaaaaaaa", "bbbbbbbbbbb"], title="share")

    async def explode(url):
        raise AssertionError("yt-dlp must not run for a playlist we already have")

    monkeypatch.setattr(bridge, "_fetch_playlist", explode)
    asyncio.run(bridge._load_playlist(SOURCE))  # noqa: SLF001

    loadlist = [call for call in ipc.calls if call[0] == "loadlist"]
    assert len(loadlist) == 1
    assert loadlist[0][1] == str(bridge.cache.m3u_path(SOURCE))
    assert loadlist[0][2] == "replace"
    assert bridge.source_url == SOURCE
    assert bridge.source_title == "share"


def test_loading_unpauses(tmp_path):
    """`stop` from the notification is a pause, so a fresh load would otherwise sit silent."""
    ipc, _, bridge = make_bridge(tmp_path)
    bridge.cache.save_playlist(SOURCE, ["aaaaaaaaaaa"], title="share")
    asyncio.run(bridge._load_playlist(SOURCE))  # noqa: SLF001

    assert ("set_property", "pause", False) in ipc.calls


def test_loading_an_unknown_playlist_resolves_it_first(monkeypatch, tmp_path):
    ipc, server, bridge = make_bridge(tmp_path)

    async def fake_fetch(url):
        return "share", [("aaaaaaaaaaa", "First")]

    monkeypatch.setattr(bridge, "_fetch_playlist", fake_fetch)
    monkeypatch.setattr("mpvbridge.bridge.shutil.which", lambda _: "/usr/bin/yt-dlp")
    asyncio.run(bridge._load_playlist(SOURCE))  # noqa: SLF001

    assert [call for call in ipc.calls if call[0] == "loadlist"]
    assert bridge.cache.load_playlist(SOURCE) == ["aaaaaaaaaaa"]
    assert library_of(server)[0]["current"] is True


def test_loading_paints_the_new_playlist_before_mpv_expands_it(tmp_path):
    _, server, bridge = make_bridge(tmp_path)
    bridge.cache.save_playlist(SOURCE, ["aaaaaaaaaaa", "bbbbbbbbbbb"], title="share")
    bridge.cache.remember_titles({"aaaaaaaaaaa": "First", "bbbbbbbbbbb": "Second"})
    asyncio.run(bridge._load_playlist(SOURCE))  # noqa: SLF001

    playlist = [m for m in server.broadcasts if m["type"] == "playlist"][-1]
    assert [entry["title"] for entry in playlist["entries"]] == ["First", "Second"]


def test_the_state_message_names_the_playlist_it_came_from(tmp_path):
    """mpv rewrites playlist-path once a playlist is expanded, so the app cannot ask mpv."""
    _, _, bridge = make_bridge(tmp_path, source_url=SOURCE)
    bridge.cache.save_playlist(SOURCE, ["aaaaaaaaaaa"], title="share")
    bridge.source_title = "share"

    assert bridge.state_message()["source"] == {"url": SOURCE, "title": "share"}


def test_the_state_message_has_no_source_for_loose_files(tmp_path):
    _, _, bridge = make_bridge(tmp_path)

    assert bridge.state_message()["source"] is None


def test_a_failed_load_keeps_the_playlist_mpv_actually_has(tmp_path):
    """mpv still has the old one, so the library must not start claiming otherwise."""

    class RefusingIpc(FakeIpc):
        async def command(self, *args):
            if args and args[0] == "loadlist":
                raise MpvIpcError("no such file")
            return await super().command(*args)

    server = FakeServer()
    bridge = Bridge(RefusingIpc(), server, source_url=OTHER, cache=PlaylistCache(tmp_path))
    bridge.cache.save_playlist(OTHER, ["ccccccccccc"], title="playing now")
    bridge.cache.save_playlist(SOURCE, ["aaaaaaaaaaa"], title="share")
    bridge.source_title = "playing now"

    asyncio.run(bridge._load_playlist(SOURCE))  # noqa: SLF001

    assert bridge.source_url == OTHER
    assert bridge.source_title == "playing now"
    assert [p for p in library_of(server) if p["current"]][0]["title"] == "playing now"


# -- removing ----------------------------------------------------------------------------------


def test_removing_the_playing_playlist_leaves_playback_alone(tmp_path):
    ipc, server, bridge = make_bridge(tmp_path, source_url=SOURCE)
    bridge.cache.save_playlist(SOURCE, ["aaaaaaaaaaa"], title="share")
    asyncio.run(bridge._remove_playlist(SOURCE))  # noqa: SLF001

    assert ipc.calls == []
    assert library_of(server) == []
    assert bridge.source_url is None


# -- one job at a time ---------------------------------------------------------------------------


def test_a_second_playlist_job_is_refused_while_one_runs(monkeypatch, tmp_path):
    """Two yt-dlp runs at once would fight over the same cache files."""
    _, _, bridge = make_bridge(tmp_path)

    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()
        ran = []

        async def slow():
            ran.append("first")
            started.set()
            await release.wait()

        async def second():
            ran.append("second")

        bridge._start_task(slow())  # noqa: SLF001
        await started.wait()
        bridge._start_task(second())  # noqa: SLF001
        release.set()
        await bridge._refresh_task  # noqa: SLF001
        return ran

    assert asyncio.run(scenario()) == ["first"]


# -- shuffle must not become the remembered order ------------------------------------------------


def test_shuffling_does_not_rewrite_the_cached_order(tmp_path):
    """A shuffle is a view, not the playlist. Saving it would break the next launch."""
    _, _, bridge = make_bridge(tmp_path, source_url=SOURCE)
    bridge.cache.save_playlist(SOURCE, ["aaaaaaaaaaa", "bbbbbbbbbbb"], title="share")

    asyncio.run(bridge.handle_command("shuffle", {"type": "cmd", "name": "shuffle"}))
    shuffled = [
        {"filename": "https://www.youtube.com/watch?v=bbbbbbbbbbb", "title": "Second"},
        {"filename": "https://www.youtube.com/watch?v=aaaaaaaaaaa", "title": "First"},
    ]
    bridge._remember(build_playlist(shuffled))  # noqa: SLF001

    assert bridge.cache.load_playlist(SOURCE) == ["aaaaaaaaaaa", "bbbbbbbbbbb"]


def test_unshuffling_lets_the_order_be_remembered_again(tmp_path):
    _, _, bridge = make_bridge(tmp_path, source_url=SOURCE)
    bridge.cache.save_playlist(SOURCE, ["aaaaaaaaaaa", "bbbbbbbbbbb"], title="share")

    asyncio.run(bridge.handle_command("shuffle", {"type": "cmd", "name": "shuffle"}))
    asyncio.run(bridge.handle_command("unshuffle", {"type": "cmd", "name": "unshuffle"}))
    bridge._remember(  # noqa: SLF001
        build_playlist(
            [
                {"filename": "https://www.youtube.com/watch?v=ccccccccccc", "title": "Third"},
                {"filename": "https://www.youtube.com/watch?v=aaaaaaaaaaa", "title": "First"},
            ]
        )
    )

    assert bridge.cache.load_playlist(SOURCE) == ["ccccccccccc", "aaaaaaaaaaa"]


# -- idle --------------------------------------------------------------------------------------


def test_no_media_argument_means_idle():
    """Without --idle mpv exits the instant it finds no files, taking the bridge with it."""
    assert wants_idle(["--vo=null"], None, forced=False) is True


def test_a_playlist_url_means_not_idle():
    assert wants_idle(["--vo=null", SOURCE], SOURCE, forced=False) is False


def test_bridge_idle_forces_it_even_with_a_url():
    assert wants_idle(["--vo=null", SOURCE], SOURCE, forced=True) is True


def test_an_explicit_mpv_idle_option_is_left_alone():
    assert wants_idle(["--idle=once"], None, forced=True) is False
