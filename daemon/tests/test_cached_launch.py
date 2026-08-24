"""Starting mpv from a cached playlist file instead of re-resolving the URL every launch."""

from __future__ import annotations

from mpvbridge.__main__ import source_url_for, use_cached_playlist
from mpvbridge.bridge import Bridge
from mpvbridge.cache import PlaylistCache
from mpvbridge.protocol import Playlist, PlaylistEntry

SOURCE = "https://www.youtube.com/playlist?list=PLFx"
ARGS = ["--shuffle", "--vo=null", SOURCE]


class FakeIpc:
    def start(self, on_event):
        pass


class FakeServer:
    async def broadcast(self, message):
        pass


def warm_cache(tmp_path, ids=("aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc")):
    cache = PlaylistCache(tmp_path)
    cache.save_playlist(SOURCE, list(ids))
    return cache


def test_a_warm_cache_replaces_the_url_with_a_local_playlist(tmp_path):
    cache = warm_cache(tmp_path)

    args = use_cached_playlist(ARGS, SOURCE, cache)

    assert SOURCE not in args
    assert "--shuffle" in args and "--vo=null" in args
    playlist_arg = [a for a in args if a.startswith("--playlist=")]
    assert len(playlist_arg) == 1
    written = cache.m3u_path(SOURCE).read_text(encoding="utf-8")
    assert "watch?v=aaaaaaaaaaa" in written
    assert written.count("watch?v=") == 3


def test_a_cold_cache_leaves_the_url_alone(tmp_path):
    args = use_cached_playlist(ARGS, SOURCE, PlaylistCache(tmp_path))

    assert args == ARGS


def test_a_single_entry_cache_is_not_worth_using(tmp_path):
    cache = warm_cache(tmp_path, ids=("aaaaaaaaaaa",))

    assert use_cached_playlist(ARGS, SOURCE, cache) == ARGS


def test_a_non_playlist_url_is_left_alone(tmp_path):
    single = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    cache = warm_cache(tmp_path)

    assert use_cached_playlist(["--shuffle", single], single, cache) == ["--shuffle", single]


def test_local_files_are_left_alone(tmp_path):
    args = ["--shuffle", "/storage/music/album"]

    assert use_cached_playlist(args, "/storage/music/album", warm_cache(tmp_path)) == args


def test_source_url_survives_the_rewrite(tmp_path):
    """The original URL has to be kept for refresh; mpv only ever sees the local file."""
    cache = warm_cache(tmp_path)
    rewritten = use_cached_playlist(ARGS, SOURCE, cache)

    assert source_url_for(ARGS) == SOURCE
    assert source_url_for(rewritten) is None


def test_cached_titles_rescue_entries_mpv_has_not_resolved(tmp_path):
    """Booting from a local playlist means mpv knows no titles, so without the cache overlay
    every row would look like an unavailable bare URL."""
    cache = PlaylistCache(tmp_path)
    cache.remember_titles({"aaaaaaaaaaa": "First Song", "bbbbbbbbbbb": "Second Song"})
    bridge = Bridge(FakeIpc(), FakeServer(), source_url=SOURCE, cache=cache)

    playlist = Playlist(
        entries=[
            PlaylistEntry(0, "https://www.youtube.com/watch?v=aaaaaaaaaaa", False,
                          "https://www.youtube.com/watch?v=aaaaaaaaaaa", True),
            PlaylistEntry(1, "https://www.youtube.com/watch?v=bbbbbbbbbbb", False,
                          "https://www.youtube.com/watch?v=bbbbbbbbbbb", True),
            PlaylistEntry(2, "https://www.youtube.com/watch?v=zzzzzzzzzzz", False,
                          "https://www.youtube.com/watch?v=zzzzzzzzzzz", True),
        ]
    )
    bridge._enrich(playlist)  # noqa: SLF001

    assert [e.title for e in playlist.entries[:2]] == ["First Song", "Second Song"]
    assert [e.unavailable for e in playlist.entries] == [False, False, True]


def test_enrichment_never_overwrites_a_title_mpv_resolved(tmp_path):
    cache = PlaylistCache(tmp_path)
    cache.remember_titles({"aaaaaaaaaaa": "Stale cached title"})
    bridge = Bridge(FakeIpc(), FakeServer(), source_url=SOURCE, cache=cache)

    playlist = Playlist(
        entries=[
            PlaylistEntry(0, "Fresh title from mpv", False,
                          "https://www.youtube.com/watch?v=aaaaaaaaaaa", False)
        ]
    )
    bridge._enrich(playlist)  # noqa: SLF001

    assert playlist.entries[0].title == "Fresh title from mpv"
