"""Wires the mpv IPC socket to the TCP server."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from typing import Any

from . import art
from .cache import PlaylistCache
from .ipc import MpvIpc, MpvIpcError
from .protocol import (
    CMD_ADD_PLAYLIST,
    CMD_GOTO,
    CMD_LIBRARY,
    CMD_LOAD_PLAYLIST,
    CMD_NEXT,
    CMD_PAUSE,
    CMD_PLAY,
    CMD_PLAY_URL,
    CMD_PREVIOUS,
    CMD_REFRESH,
    CMD_REFRESH_PLAYLIST,
    CMD_REMOVE_PLAYLIST,
    CMD_SEEK,
    CMD_SHUFFLE,
    CMD_STOP,
    CMD_TOGGLE,
    CMD_UNSHUFFLE,
    REFRESH_KIND_ADD,
    REFRESH_KIND_LOAD,
    REFRESH_KIND_RECHECK,
    Playlist,
    PlaylistEntry,
    SavedPlaylist,
    hello_message,
    library_message,
    refresh_message,
)
from .server import BridgeServer
from .state import (
    OBSERVED_PROPERTIES,
    StateTracker,
    build_playlist,
    is_unavailable_title,
)

log = logging.getLogger(__name__)

#: A single track change fires path, media-title, metadata, duration and playlist-pos in quick
#: succession. Coalescing them into one broadcast keeps the app from redrawing five times.
BROADCAST_DEBOUNCE_SECONDS = 0.08

#: No mpv request should ever outlive this. A hung reply must not wedge the whole bridge.
IPC_TIMEOUT_SECONDS = 5.0

#: yt-dlp on an 855-entry playlist is slow. Generous, but not unbounded.
REFRESH_TIMEOUT_SECONDS = 240.0

WATCH_URL = "https://www.youtube.com/watch?v={}"

#: mpv events that mean the snapshot is stale. They are listed rather than handled individually
#: because the response is the same for all of them: re-read state and tell the app.
DIRTYING_EVENTS = frozenset(
    {"start-file", "playback-restart", "file-loaded", "end-file", "shutdown"}
)


class Bridge:
    def __init__(
        self,
        ipc: MpvIpc,
        server: BridgeServer,
        source_url: str | None = None,
        cache: PlaylistCache | None = None,
    ) -> None:
        self.ipc = ipc
        self.server = server
        self.tracker = StateTracker()
        self.mpv_version: str | None = None
        self.source_url = source_url
        self.cache = cache or PlaylistCache()
        self.source_title = self.cache.playlist_title(source_url) if source_url else None
        self._refresh_task: asyncio.Task[None] | None = None
        self._playlist_message: dict[str, Any] | None = None
        #: mpv's playlist is in a shuffled order. The cache must not learn that order, or the
        #: next launch would start from the shuffle and `unshuffle` would have nothing to undo.
        self._shuffled = False
        self._dirty = asyncio.Event()
        self._playlist_dirty = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        # The reader has to exist before anything is asked of mpv: a command awaits a reply that
        # only the reader task can resolve.
        self.ipc.start(self._on_event)

        with contextlib.suppress(MpvIpcError, ConnectionError, TimeoutError):
            self.mpv_version = await asyncio.wait_for(
                self.ipc.get_property("mpv-version"), timeout=IPC_TIMEOUT_SECONDS
            )

        for name in OBSERVED_PROPERTIES:
            with contextlib.suppress(MpvIpcError, ConnectionError, TimeoutError):
                await asyncio.wait_for(
                    self.ipc.observe_property(name), timeout=IPC_TIMEOUT_SECONDS
                )

        self._seed_playlist_from_cache()

        self._tasks = [
            asyncio.create_task(self._broadcast_loop(), name="bridge-broadcast"),
            asyncio.create_task(self._playlist_loop(), name="bridge-playlist"),
        ]
        self._playlist_dirty.set()

    def _seed_playlist_from_cache(self) -> None:
        """Publish the last known contents of this playlist before mpv has expanded it.

        yt-dlp takes seconds to resolve a large playlist, during which mpv reports a single
        unexpanded entry. Anything mpv later reports supersedes this.
        """
        if not self.source_url:
            self._playlist_message = None
            return
        ids = self.cache.load_playlist(self.source_url)
        if not ids:
            self._playlist_message = None
            return
        entries = [
            PlaylistEntry(
                index=index,
                title=self.cache.title_for(video_id) or WATCH_URL.format(video_id),
                current=False,
                url=WATCH_URL.format(video_id),
                unavailable=self.cache.title_for(video_id) is None,
            )
            for index, video_id in enumerate(ids)
        ]
        self._playlist_message = Playlist(entries=entries).to_message()
        log.info("seeded %d playlist entries from cache", len(entries))

    async def stop(self) -> None:
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._refresh_task
            self._refresh_task = None
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

    # -- snapshots ---------------------------------------------------------------------------

    def snapshot_messages(self) -> list[dict[str, Any]]:
        """Everything a freshly connected client needs, in order."""
        messages: list[dict[str, Any]] = [hello_message(self.mpv_version)]
        messages.append(self.state_message())
        if self._playlist_message is not None:
            messages.append(self._playlist_message)
        messages.append(self.library_message())
        return messages

    def state_message(self) -> dict[str, Any]:
        """The player snapshot, stamped with which playlist it came from.

        The tracker only knows what mpv reports, and mpv rewrites ``playlist-path`` the moment a
        playlist is expanded -- so the source has to be carried alongside rather than read back.
        """
        state = self.tracker.snapshot()
        state.source_url = self.source_url
        state.source_title = self.source_title
        return state.to_message()

    def library_message(self) -> dict[str, Any]:
        saved = [
            SavedPlaylist(
                url=entry["url"],
                title=entry["title"],
                count=entry["count"],
                fetched=entry["fetched"],
                current=entry["url"] == self.source_url,
            )
            for entry in self.cache.known_playlists()
        ]
        return library_message(saved)

    # -- mpv -> clients ----------------------------------------------------------------------

    async def _on_event(self, message: dict[str, Any]) -> None:
        event = message.get("event")
        if event == "property-change":
            name = message.get("name")
            if not isinstance(name, str):
                return
            if self.tracker.update(name, message.get("data")):
                self._dirty.set()
            if name == "playlist-count":
                # Only the length matters here. Which entry is current comes from playlist-pos in
                # the state message, so a position change needs no refetch -- refetching 855
                # entries on every track change would be pure waste.
                self._playlist_dirty.set()
        elif event in DIRTYING_EVENTS:
            self._dirty.set()

    async def _broadcast_loop(self) -> None:
        while True:
            await self._dirty.wait()
            await asyncio.sleep(BROADCAST_DEBOUNCE_SECONDS)
            self._dirty.clear()
            await self.server.broadcast(self.state_message())

    async def _playlist_loop(self) -> None:
        while True:
            await self._playlist_dirty.wait()
            await asyncio.sleep(BROADCAST_DEBOUNCE_SECONDS)
            self._playlist_dirty.clear()
            try:
                raw = await asyncio.wait_for(
                    self.ipc.get_property("playlist"), timeout=IPC_TIMEOUT_SECONDS
                )
            except (MpvIpcError, ConnectionError, TimeoutError):
                continue
            except asyncio.CancelledError:
                raise
            current = self.tracker.properties.get("playlist-pos")
            playlist = build_playlist(raw, current if isinstance(current, int) else None)
            self._enrich(playlist)
            self._playlist_message = playlist.to_message()
            self._remember(playlist)
            await self.server.broadcast(self._playlist_message)

    def _enrich(self, playlist: Playlist) -> None:
        """Fill in titles mpv does not know from the cache.

        When mpv is started from a local playlist file it has no titles at all until it plays each
        entry, so without this every row would look like an unavailable bare URL. Entries that are
        genuinely dead were never cached with a title, so they stay flagged.
        """
        for entry in playlist.entries:
            if not entry.unavailable:
                continue
            video_id = art.youtube_id(entry.url)
            if video_id is None:
                continue
            cached = self.cache.title_for(video_id)
            if cached and not is_unavailable_title(cached):
                entry.title = cached
                entry.unavailable = False

    def _remember(self, playlist: Playlist) -> None:
        if not self.source_url or len(playlist.entries) < 2:
            return
        ids: list[str] = []
        titles: dict[str, str] = {}
        for entry in playlist.entries:
            video_id = art.youtube_id(entry.url)
            if video_id is None:
                continue
            ids.append(video_id)
            if not entry.unavailable:
                titles[video_id] = entry.title
        if ids and not self._shuffled:
            self.cache.save_playlist(self.source_url, ids)
            self.cache.write_m3u(self.source_url, ids)
        if titles:
            self.cache.remember_titles(titles)

    # -- clients -> mpv ----------------------------------------------------------------------

    async def handle_command(self, name: str, message: dict[str, Any]) -> None:
        log.info("command from app: %s %s", name, {k: v for k, v in message.items() if k != "type"})
        try:
            await asyncio.wait_for(self._dispatch(name, message), timeout=IPC_TIMEOUT_SECONDS)
        except (MpvIpcError, ConnectionError, TimeoutError) as exc:
            log.warning("command %s failed: %s", name, exc)

    async def _dispatch(self, name: str, message: dict[str, Any]) -> None:
        # Real mpv commands rather than simulated keypresses, so none of this depends on the
        # user's input.conf or on mpv having terminal focus.
        if name == CMD_PLAY:
            await self.ipc.set_property("pause", False)
        elif name == CMD_PAUSE:
            await self.ipc.set_property("pause", True)
        elif name == CMD_TOGGLE:
            await self.ipc.command("cycle", "pause")
        elif name == CMD_NEXT:
            await self.ipc.command("playlist-next", "weak")
        elif name == CMD_PREVIOUS:
            await self.ipc.command("playlist-prev", "weak")
        elif name == CMD_STOP:
            # Deliberately a pause, not mpv's `stop`: dismissing a notification should not throw
            # away the loaded playlist.
            await self.ipc.set_property("pause", True)
        elif name == CMD_SEEK:
            position = message.get("position")
            if isinstance(position, (int, float)):
                await self.ipc.command("seek", float(position), "absolute")
        elif name == CMD_GOTO:
            index = message.get("index")
            if isinstance(index, int) and index >= 0:
                await self.ipc.set_property("playlist-pos", index)
        elif name == CMD_SHUFFLE:
            await self.ipc.command("playlist-shuffle")
            self._shuffled = True
            self._playlist_dirty.set()
        elif name == CMD_UNSHUFFLE:
            # Only restores order for a shuffle mpv itself performed; a playlist started with
            # --shuffle has no original order to go back to.
            await self.ipc.command("playlist-unshuffle")
            self._shuffled = False
            self._playlist_dirty.set()
        elif name == CMD_PLAY_URL:
            url = message.get("url")
            if isinstance(url, str) and url.strip():
                await self._play_url(url.strip())
        elif name == CMD_REFRESH_PLAYLIST:
            self._start_task(self._refresh_playlist())
        elif name == CMD_LIBRARY:
            await self.server.broadcast(self.library_message())
        elif name == CMD_ADD_PLAYLIST:
            url = message.get("url")
            if isinstance(url, str) and url.strip():
                self._start_task(self._add_playlist(url.strip()))
        elif name == CMD_LOAD_PLAYLIST:
            url = message.get("url")
            if isinstance(url, str) and url.strip():
                self._start_task(self._load_playlist(url.strip()))
        elif name == CMD_REMOVE_PLAYLIST:
            url = message.get("url")
            if isinstance(url, str) and url.strip():
                await self._remove_playlist(url.strip())
        elif name == CMD_REFRESH:
            self._dirty.set()
            self._playlist_dirty.set()

    async def _play_url(self, url: str) -> None:
        """Play *url* now without discarding the loaded playlist.

        ``replace`` would throw away an 855-entry playlist, so this inserts after the current
        entry instead. ``insert-next-play`` is newer than some mpv builds, so fall back rather
        than fail.
        """
        try:
            await self.ipc.command("loadfile", url, "insert-next-play")
        except MpvIpcError as exc:
            log.info("insert-next-play rejected (%s); falling back to append-play", exc)
            await self.ipc.command("loadfile", url, "append-play")

    # -- playlist refresh --------------------------------------------------------------------

    def _start_task(self, coroutine: Any) -> None:
        """Run one yt-dlp-backed job at a time.

        Adding, loading and re-checking a playlist all end up shelling out to yt-dlp, and running
        two of those at once would have them fighting over the same cache files.
        """
        if self._refresh_task is not None and not self._refresh_task.done():
            log.info("a playlist job is already running; ignoring")
            coroutine.close()
            return
        self._refresh_task = asyncio.create_task(coroutine, name="bridge-playlist-job")

    async def _refresh_playlist(self) -> None:
        """Re-read the current source playlist and append whatever mpv does not already have."""
        if not self.source_url:
            await self.server.broadcast(
                refresh_message("error", reason="no source playlist URL to re-check")
            )
            return

        resolved = await self._resolve(self.source_url, REFRESH_KIND_RECHECK)
        if resolved is None:
            return
        title, fetched = resolved

        known = await self._current_video_ids()
        new = [video_id for video_id, _ in fetched if video_id not in known]

        for video_id in new:
            with contextlib.suppress(MpvIpcError, ConnectionError):
                await self.ipc.command("loadfile", WATCH_URL.format(video_id), "append")

        log.info("refresh: %d in source, %d new", len(fetched), len(new))
        await self.server.broadcast(
            refresh_message(
                "done",
                added=len(new),
                total=len(fetched),
                title=title,
                url=self.source_url,
                kind=REFRESH_KIND_RECHECK,
            )
        )
        self._playlist_dirty.set()
        await self.server.broadcast(self.library_message())

    async def _add_playlist(self, url: str) -> None:
        """Resolve a playlist the app has just been handed, so it can be named and played later."""
        resolved = await self._resolve(url, REFRESH_KIND_ADD)
        if resolved is None:
            return
        title, fetched = resolved
        log.info("added %s (%d entries)", title or url, len(fetched))
        await self.server.broadcast(
            refresh_message(
                "done", total=len(fetched), title=title, url=url, kind=REFRESH_KIND_ADD
            )
        )
        await self.server.broadcast(self.library_message())

    async def _load_playlist(self, url: str) -> None:
        """Switch mpv to *url*, resolving it with yt-dlp only if we have never seen it.

        This is what lets the app be the way in: mpv can be started idle with no arguments and
        told what to play from the phone, instead of the playlist being fixed on the command line.
        """
        ids = self.cache.load_playlist(url)
        title = self.cache.playlist_title(url)
        resolved_now = False
        if not ids:
            resolved = await self._resolve(url, REFRESH_KIND_LOAD)
            if resolved is None:
                return
            title, fetched = resolved
            ids = [video_id for video_id, _ in fetched]
            resolved_now = True

        path = self.cache.write_m3u(url, ids)
        if path is None:
            await self.server.broadcast(
                refresh_message(
                    "error",
                    reason="could not write the playlist file",
                    url=url,
                    kind=REFRESH_KIND_LOAD,
                )
            )
            return

        previous = (self.source_url, self.source_title)
        self.source_url = url
        self.source_title = title
        self._shuffled = False
        # Paint the new playlist immediately; mpv takes a moment to expand the file.
        self._seed_playlist_from_cache()

        try:
            await self.ipc.command("loadlist", str(path), "replace")
            # mpv keeps `pause` across a load, and `stop` from the notification is a pause -- so
            # without this, picking a playlist would load it and then sit there silently.
            await self.ipc.set_property("pause", False)
        except (MpvIpcError, ConnectionError, TimeoutError) as exc:
            # mpv still has the old playlist, so we must not go on claiming it has the new one.
            log.warning("could not load %s: %s", path, exc)
            self.source_url, self.source_title = previous
            self._seed_playlist_from_cache()
            await self.server.broadcast(
                refresh_message("error", reason=str(exc), url=url, kind=REFRESH_KIND_LOAD)
            )
            await self.server.broadcast(self.library_message())
            return

        log.info("loaded %d entries from %s", len(ids), title or url)
        if resolved_now:
            await self.server.broadcast(
                refresh_message(
                    "done", total=len(ids), title=title, url=url, kind=REFRESH_KIND_LOAD
                )
            )
        if self._playlist_message is not None:
            await self.server.broadcast(self._playlist_message)
        await self.server.broadcast(self.state_message())
        await self.server.broadcast(self.library_message())
        self._dirty.set()
        self._playlist_dirty.set()

    async def _remove_playlist(self, url: str) -> None:
        self.cache.forget_playlist(url)
        if url == self.source_url:
            # Nothing left to re-check, but whatever mpv already has loaded keeps playing.
            self.source_url = None
            self.source_title = None
        await self.server.broadcast(self.library_message())

    async def _resolve(
        self, url: str, kind: str
    ) -> tuple[str | None, list[tuple[str, str]]] | None:
        """Run yt-dlp over *url* and cache what comes back.

        None means it failed and the failure has already been reported to the app.
        """
        if shutil.which("yt-dlp") is None:
            await self.server.broadcast(
                refresh_message("error", reason="yt-dlp is not installed", url=url, kind=kind)
            )
            return None

        await self.server.broadcast(
            refresh_message(
                "running", url=url, title=self.cache.playlist_title(url), kind=kind
            )
        )
        try:
            title, fetched = await self._fetch_playlist(url)
        except TimeoutError:
            await self.server.broadcast(
                refresh_message("error", reason="yt-dlp timed out", url=url, kind=kind)
            )
            return None
        except OSError as exc:
            await self.server.broadcast(
                refresh_message("error", reason=str(exc), url=url, kind=kind)
            )
            return None

        if not fetched:
            await self.server.broadcast(
                refresh_message("error", reason="yt-dlp returned nothing", url=url, kind=kind)
            )
            return None

        # A dead video's "title" is a placeholder like "[Deleted video]". Caching that would make
        # it look resolved, and `Hide unavailable tracks` would quietly stop hiding it.
        self.cache.remember_titles(
            {vid: name for vid, name in fetched if name and not is_unavailable_title(name)}
        )
        ids = [video_id for video_id, _ in fetched]
        self.cache.save_playlist(url, ids, title=title)
        # Keep the local playlist file in step, so the next launch starts from the new contents.
        self.cache.write_m3u(url, ids)
        if url == self.source_url and title:
            self.source_title = title
        return title, fetched

    async def _fetch_playlist(self, url: str) -> tuple[str | None, list[tuple[str, str]]]:
        """Ask yt-dlp for a playlist as its own title plus (video id, title) pairs.

        The playlist title rides along on every entry of ``--flat-playlist`` output, so naming the
        playlist costs no extra call -- which is how the app learns a pasted URL is "share".
        """
        process = await asyncio.create_subprocess_exec(
            "yt-dlp",
            "--flat-playlist",
            "--ignore-errors",
            "--no-warnings",
            "--print",
            "%(playlist_title)s\t%(id)s\t%(title)s",
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=REFRESH_TIMEOUT_SECONDS
            )
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            raise

        if process.returncode not in (0, 1):  # 1 just means some entries were skipped
            log.warning("yt-dlp exited %s: %s", process.returncode, stderr.decode()[:300])

        playlist_title: str | None = None
        pairs: list[tuple[str, str]] = []
        for line in stdout.decode("utf-8", "replace").splitlines():
            parts = line.split("\t", 2)
            if len(parts) < 3:
                continue
            name, video_id, title = (part.strip() for part in parts)
            # yt-dlp prints the literal string NA for a field it has no value for.
            if playlist_title is None and name and name != "NA":
                playlist_title = name
            if video_id and video_id != "NA":
                pairs.append((video_id, title))
        return playlist_title, pairs

    async def _current_video_ids(self) -> set[str]:
        try:
            raw = await asyncio.wait_for(
                self.ipc.get_property("playlist"), timeout=IPC_TIMEOUT_SECONDS
            )
        except (MpvIpcError, ConnectionError, TimeoutError):
            return set()
        playlist = build_playlist(raw)
        return {
            video_id
            for video_id in (art.youtube_id(entry.url) for entry in playlist.entries)
            if video_id
        }
