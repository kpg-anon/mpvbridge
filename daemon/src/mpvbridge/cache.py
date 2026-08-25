"""On-disk cache of playlist contents, so the app can paint before yt-dlp has finished.

Two files under ``$XDG_CACHE_HOME/mpvbridge`` (or ``~/.cache/mpvbridge``):

``titles.json``
    A flat ``videoId -> title`` map shared by every playlist. Titles effectively never change, so
    this only grows and never needs invalidating.

``playlists/<sha1-of-url>.json``
    The ordered video ids for one source playlist, plus its URL, its title where yt-dlp told us
    one, and when it was fetched. These files are also the app's playlist library: the only way
    to enumerate them is to read them, since they are named by a hash of the URL.

Splitting them this way means re-fetching a playlist only needs ids -- titles are resolved
locally -- and it lets the bridge answer a client with a full, titled playlist immediately on
connect, before mpv has expanded anything.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Guard against a runaway title map. Well past any realistic library.
MAX_TITLES = 100_000


def cache_root() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(Path.home(), ".cache")
    return Path(base) / "mpvbridge"


def _read_json(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError) as exc:
        # A truncated cache is not worth crashing over; it rebuilds itself.
        log.warning("discarding unreadable cache %s: %s", path, exc)
        return default


def _write_json(path: Path, payload: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename so a kill mid-write cannot leave a half-file behind.
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
        temporary.replace(path)
    except OSError as exc:
        log.warning("could not write cache %s: %s", path, exc)


class PlaylistCache:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or cache_root()
        self._titles: dict[str, str] | None = None

    # -- titles ------------------------------------------------------------------------------

    @property
    def titles_path(self) -> Path:
        return self.root / "titles.json"

    @property
    def titles(self) -> dict[str, str]:
        if self._titles is None:
            loaded = _read_json(self.titles_path, {})
            self._titles = loaded if isinstance(loaded, dict) else {}
        return self._titles

    def title_for(self, video_id: str) -> str | None:
        return self.titles.get(video_id)

    def remember_titles(self, titles: dict[str, str]) -> int:
        """Merge in newly seen titles. Returns how many were new."""
        known = self.titles
        added = 0
        for video_id, title in titles.items():
            if not video_id or not title:
                continue
            if known.get(video_id) != title:
                known[video_id] = title
                added += 1
        if added:
            if len(known) > MAX_TITLES:
                log.warning("title cache over %d entries; not growing further", MAX_TITLES)
            else:
                _write_json(self.titles_path, known)
        return added

    # -- playlists ---------------------------------------------------------------------------

    def playlist_path(self, url: str) -> Path:
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        return self.root / "playlists" / f"{digest}.json"

    def load_playlist(self, url: str) -> list[str]:
        """Ordered video ids last recorded for *url*, or an empty list."""
        payload = _read_json(self.playlist_path(url), None)
        if not isinstance(payload, dict):
            return []
        ids = payload.get("ids")
        if not isinstance(ids, list):
            return []
        return [str(entry) for entry in ids if entry]

    def save_playlist(self, url: str, ids: list[str], title: str | None = None) -> None:
        if not url or not ids:
            return
        payload: dict[str, Any] = {"url": url, "fetched": int(time.time()), "ids": ids}
        # A save triggered by mpv's own playlist knows no title; keep the one yt-dlp found.
        keep = title or self.playlist_title(url)
        if keep:
            payload["title"] = keep
        _write_json(self.playlist_path(url), payload)

    def playlist_title(self, url: str) -> str | None:
        payload = _read_json(self.playlist_path(url), None)
        if not isinstance(payload, dict):
            return None
        title = payload.get("title")
        return title if isinstance(title, str) and title.strip() else None

    def known_playlists(self) -> list[dict[str, Any]]:
        """Every playlist this cache has seen, newest fetch first.

        The per-playlist files are named by a hash of the URL, so the only way to enumerate them
        is to read them. There are a handful, not thousands.
        """
        found: list[dict[str, Any]] = []
        directory = self.root / "playlists"
        if not directory.is_dir():
            return found
        for path in sorted(directory.glob("*.json")):
            payload = _read_json(path, None)
            if not isinstance(payload, dict):
                continue
            url = payload.get("url")
            ids = payload.get("ids")
            if not isinstance(url, str) or not isinstance(ids, list):
                continue
            title = payload.get("title")
            found.append(
                {
                    "url": url,
                    "title": title if isinstance(title, str) else None,
                    "count": len(ids),
                    "fetched": int(payload.get("fetched") or 0),
                }
            )
        found.sort(key=lambda item: item["fetched"], reverse=True)
        return found

    def forget_playlist(self, url: str) -> bool:
        """Drop one playlist from the cache. Titles are shared, so they stay."""
        removed = False
        for path in (self.playlist_path(url), self.m3u_path(url)):
            try:
                path.unlink()
                removed = True
            except FileNotFoundError:
                continue
            except OSError as exc:
                log.warning("could not remove %s: %s", path, exc)
        return removed

    def m3u_path(self, url: str) -> Path:
        return self.playlist_path(url).with_suffix(".m3u")

    def write_m3u(self, url: str, ids: list[str]) -> Path | None:
        """Write the cached ids as a local playlist mpv can open without calling yt-dlp.

        Handing mpv the playlist URL makes it run a full yt-dlp flat-playlist fetch at startup --
        many seconds for a large playlist. Handing it this file instead means mpv only resolves
        each video as it reaches it.
        """
        if not url or not ids:
            return None
        path = self.m3u_path(url)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".m3u.tmp")
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write("#EXTM3U\n")
                for video_id in ids:
                    handle.write(f"https://www.youtube.com/watch?v={video_id}\n")
            temporary.replace(path)
            return path
        except OSError as exc:
            log.warning("could not write playlist file %s: %s", path, exc)
            return None

    def size_bytes(self) -> int:
        total = 0
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            try:
                total += path.stat().st_size
            except OSError:
                continue
        return total

    def clear(self) -> None:
        self._titles = None
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            try:
                path.unlink()
            except OSError as exc:
                log.debug("could not remove %s: %s", path, exc)
