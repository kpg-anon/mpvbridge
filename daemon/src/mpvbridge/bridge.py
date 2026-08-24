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
    CMD_GOTO,
    CMD_NEXT,
    CMD_PAUSE,
    CMD_PLAY,
    CMD_PREVIOUS,
    CMD_REFRESH,
    CMD_PLAY_URL,
    CMD_REFRESH_PLAYLIST,
    CMD_SEEK,
    CMD_SHUFFLE,
    CMD_STOP,
    CMD_TOGGLE,
    CMD_UNSHUFFLE,
    PlaylistEntry,
    hello_message,
    refresh_message,
)
from .server import BridgeServer
from .protocol import Playlist
from .state import OBSERVED_PROPERTIES, StateTracker, build_playlist

log = logging.getLogger(__name__)

#: A single track change fires path, media-title, metadata, duration and playlist-pos in quick
#: succession. Coalescing them into one broadcast keeps the app from redrawing five times.
BROADCAST_DEBOUNCE_SECONDS = 0.08

#: No mpv request should ever outlive this. A hung reply must not wedge the whole bridge.
IPC_TIMEOUT_SECONDS = 5.0

#: yt-dlp on an 855-entry playlist is slow. Generous, but not unbounded.
REFRESH_TIMEOUT_SECONDS = 240.0

WATCH_URL = "https://www.youtube.com/watch?v={}"


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
        self._refresh_task: asyncio.Task[None] | None = None
        self._playlist_message: dict[str, Any] | None = None
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
            return
        ids = self.cache.load_playlist(self.source_url)
        if not ids:
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
        messages.append(self.tracker.snapshot().to_message())
        if self._playlist_message is not None:
            messages.append(self._playlist_message)
        return messages

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
        elif event in ("start-file", "playback-restart", "file-loaded"):
            self._dirty.set()
        elif event == "end-file":
            self._dirty.set()
        elif event == "shutdown":
            self._dirty.set()

    async def _broadcast_loop(self) -> None:
        while True:
            await self._dirty.wait()
            await asyncio.sleep(BROADCAST_DEBOUNCE_SECONDS)
            self._dirty.clear()
            await self.server.broadcast(self.tracker.snapshot().to_message())

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
            if cached:
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
        if ids:
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
            self._playlist_dirty.set()
        elif name == CMD_UNSHUFFLE:
            # Only restores order for a shuffle mpv itself performed; a playlist started with
            # --shuffle has no original order to go back to.
            await self.ipc.command("playlist-unshuffle")
            self._playlist_dirty.set()
        elif name == CMD_PLAY_URL:
            url = message.get("url")
            if isinstance(url, str) and url.strip():
                await self._play_url(url.strip())
        elif name == CMD_REFRESH_PLAYLIST:
            self._start_refresh()
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

    def _start_refresh(self) -> None:
        if self._refresh_task is not None and not self._refresh_task.done():
            log.info("refresh already running")
            return
        self._refresh_task = asyncio.create_task(self._refresh_playlist(), name="bridge-refresh")

    async def _refresh_playlist(self) -> None:
        if not self.source_url:
            await self.server.broadcast(
                refresh_message("error", reason="no source playlist URL to re-check")
            )
            return
        if shutil.which("yt-dlp") is None:
            await self.server.broadcast(
                refresh_message("error", reason="yt-dlp is not installed")
            )
            return

        await self.server.broadcast(refresh_message("running"))
        try:
            fetched = await self._fetch_source_ids()
        except TimeoutError:
            await self.server.broadcast(
                refresh_message("error", reason="yt-dlp timed out")
            )
            return
        except OSError as exc:
            await self.server.broadcast(refresh_message("error", reason=str(exc)))
            return

        if not fetched:
            await self.server.broadcast(
                refresh_message("error", reason="yt-dlp returned nothing")
            )
            return

        self.cache.remember_titles({vid: title for vid, title in fetched if title})
        ids = [vid for vid, _ in fetched]
        self.cache.save_playlist(self.source_url, ids)
        # Keep the local playlist file in step, so the next launch starts from the new contents.
        self.cache.write_m3u(self.source_url, ids)

        known = await self._current_video_ids()
        new = [vid for vid, _ in fetched if vid not in known]

        for video_id in new:
            with contextlib.suppress(MpvIpcError, ConnectionError):
                await self.ipc.command("loadfile", WATCH_URL.format(video_id), "append")

        log.info("refresh: %d in source, %d new", len(fetched), len(new))
        await self.server.broadcast(
            refresh_message("done", added=len(new), total=len(fetched))
        )
        self._playlist_dirty.set()

    async def _fetch_source_ids(self) -> list[tuple[str, str]]:
        """Ask yt-dlp for the source playlist as (video id, title) pairs."""
        process = await asyncio.create_subprocess_exec(
            "yt-dlp",
            "--flat-playlist",
            "--ignore-errors",
            "--no-warnings",
            "--print",
            "%(id)s	%(title)s",
            self.source_url,
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

        pairs: list[tuple[str, str]] = []
        for line in stdout.decode("utf-8", "replace").splitlines():
            video_id, _, title = line.partition("	")
            video_id = video_id.strip()
            if video_id and video_id != "NA":
                pairs.append((video_id, title.strip()))
        return pairs

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
