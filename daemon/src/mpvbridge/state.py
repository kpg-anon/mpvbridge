"""Turns observed mpv properties into the snapshot the app renders."""

from __future__ import annotations

from typing import Any

from . import art
from .protocol import PlayerState, Playlist, PlaylistEntry

#: Properties the bridge observes. ``time-pos`` is stored but never triggers a broadcast on its
#: own -- the app extrapolates the seek bar between the anchors we do send, which keeps the socket
#: idle during playback.
OBSERVED_PROPERTIES = (
    "pause",
    "media-title",
    "metadata",
    "path",
    "playlist-pos",
    "playlist-count",
    "duration",
    "time-pos",
    "idle-active",
    "eof-reached",
)

#: A change to any of these is worth pushing to connected clients.
BROADCAST_PROPERTIES = frozenset(OBSERVED_PROPERTIES) - {"time-pos"}

_ARTIST_KEYS = ("artist", "album_artist", "albumartist", "uploader", "channel")
_ALBUM_KEYS = ("album", "playlist_title", "playlist")
_TITLE_KEYS = ("icy-title", "title")

#: What yt-dlp leaves behind for videos that no longer play. mpv passes these through verbatim.
_UNAVAILABLE_TITLES = frozenset(
    {
        "[deleted video]",
        "[private video]",
        "[unavailable video]",
        "[video unavailable]",
        "deleted video",
        "private video",
    }
)


def is_unavailable_title(title: str | None) -> bool:
    """True when *title* is one of yt-dlp's placeholders for a video that no longer plays.

    These reach us from two directions -- mpv's playlist, and yt-dlp's own output -- so the test
    has to be shared, or one path would cache a placeholder the other treats as a dead entry.
    """
    return bool(title) and title.strip().lower() in _UNAVAILABLE_TITLES


def _lookup(metadata: dict[str, Any] | None, keys: tuple[str, ...]) -> str | None:
    """Case-insensitive metadata lookup; mpv passes tag names through verbatim."""
    if not metadata:
        return None
    lowered = {str(key).lower(): value for key, value in metadata.items()}
    for key in keys:
        value = lowered.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


class StateTracker:
    """Holds the last value of every observed property."""

    def __init__(self) -> None:
        self.properties: dict[str, Any] = {}
        self._art_path: str | None = None
        self._art: dict[str, str] | None = None

    def update(self, name: str, value: Any) -> bool:
        """Record a property change. Returns True if clients should be told."""
        previous = self.properties.get(name, _MISSING)
        self.properties[name] = value
        if previous is not _MISSING and previous == value:
            return False
        return name in BROADCAST_PROPERTIES

    @property
    def path(self) -> str | None:
        value = self.properties.get("path")
        return value if isinstance(value, str) else None

    def _resolve_art(self) -> dict[str, str] | None:
        path = self.path
        if path != self._art_path:
            self._art_path = path
            self._art = art.resolve(path)
        return self._art

    def snapshot(self) -> PlayerState:
        props = self.properties
        metadata = props.get("metadata") if isinstance(props.get("metadata"), dict) else None

        # media-title is mpv's own resolution of tag title -> yt-dlp title -> filename, so it is
        # already the right answer in every case we care about.
        title = props.get("media-title")
        if not isinstance(title, str) or not title.strip():
            title = _lookup(metadata, _TITLE_KEYS) or self.path or ""

        duration = props.get("duration")
        position = props.get("time-pos")

        paused = bool(props.get("pause", True))
        idle = bool(props.get("idle-active", False))

        return PlayerState(
            playing=not paused and not idle,
            title=title.strip(),
            artist=_lookup(metadata, _ARTIST_KEYS),
            album=_lookup(metadata, _ALBUM_KEYS),
            index=int(props.get("playlist-pos") or 0),
            count=int(props.get("playlist-count") or 0),
            position=float(position) if isinstance(position, (int, float)) else 0.0,
            duration=float(duration) if isinstance(duration, (int, float)) else None,
            art=self._resolve_art(),
            idle=idle,
            url=self.path,
        )


def _resolve_entry(item: dict[str, Any], index: int) -> tuple[str, bool]:
    """Return the display title for a playlist entry and whether it is unavailable.

    A remote entry that mpv never resolved a title for is a deleted, private or blocked video --
    the list shows a bare URL. A *local* file legitimately has no title, so the missing-title rule
    only applies to http(s) entries.
    """
    filename = item.get("filename")
    filename = filename.strip() if isinstance(filename, str) else ""
    is_remote = filename.startswith(("http://", "https://"))

    title = item.get("title")
    if isinstance(title, str) and title.strip():
        cleaned = title.strip()
        if is_unavailable_title(cleaned):
            return cleaned, True
        if is_remote and cleaned == filename:
            return cleaned, True
        return cleaned, False

    if filename:
        return filename, is_remote
    return f"Track {index + 1}", False


def build_playlist(raw: Any, current_index: int | None = None) -> Playlist:
    """Convert mpv's ``playlist`` property into the app's playlist message."""
    if not isinstance(raw, list):
        return Playlist()

    entries: list[PlaylistEntry] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        title, unavailable = _resolve_entry(item, index)
        filename = item.get("filename")
        is_current = bool(item.get("current")) or (
            current_index is not None and index == current_index
        )
        entries.append(
            PlaylistEntry(
                index=index,
                title=title,
                current=is_current,
                url=filename if isinstance(filename, str) else None,
                unavailable=unavailable,
            )
        )
    return Playlist(entries=entries)


class _Missing:
    __slots__ = ()


_MISSING = _Missing()
