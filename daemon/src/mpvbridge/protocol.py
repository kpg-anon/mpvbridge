"""Wire format shared with the Android companion app.

Newline-delimited JSON in both directions over a loopback TCP socket. Every message is a JSON
object with a ``type`` key. See ``docs/protocol.md`` for the full contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PROTOCOL_VERSION = 1
DEFAULT_PORT = 7355
DEFAULT_HOST = "127.0.0.1"

# server -> client
SERVER_HELLO = "hello"
SERVER_STATE = "state"
SERVER_PLAYLIST = "playlist"
SERVER_BYE = "bye"
SERVER_REFRESH = "refresh"
SERVER_LIBRARY = "library"
SERVER_ERROR = "error"

# client -> server
CLIENT_HELLO = "hello"
CLIENT_COMMAND = "cmd"

# command names a client may send
CMD_PLAY = "play"
CMD_PAUSE = "pause"
CMD_TOGGLE = "toggle"
CMD_NEXT = "next"
CMD_PREVIOUS = "previous"
CMD_STOP = "stop"
CMD_SEEK = "seek"
CMD_GOTO = "goto"
CMD_REFRESH = "refresh"
CMD_PLAY_URL = "play-url"
CMD_REFRESH_PLAYLIST = "refresh-playlist"
CMD_SHUFFLE = "shuffle"
CMD_UNSHUFFLE = "unshuffle"
CMD_LIBRARY = "library"
CMD_ADD_PLAYLIST = "add-playlist"
CMD_LOAD_PLAYLIST = "load-playlist"
CMD_REMOVE_PLAYLIST = "remove-playlist"

COMMAND_NAMES = frozenset(
    {
        CMD_PLAY,
        CMD_PAUSE,
        CMD_TOGGLE,
        CMD_NEXT,
        CMD_PREVIOUS,
        CMD_STOP,
        CMD_SEEK,
        CMD_GOTO,
        CMD_REFRESH,
        CMD_PLAY_URL,
        CMD_REFRESH_PLAYLIST,
        CMD_SHUFFLE,
        CMD_UNSHUFFLE,
        CMD_LIBRARY,
        CMD_ADD_PLAYLIST,
        CMD_LOAD_PLAYLIST,
        CMD_REMOVE_PLAYLIST,
    }
)


@dataclass(slots=True)
class PlayerState:
    """The snapshot the app renders. Field names are the wire names."""

    playing: bool = False
    title: str = ""
    artist: str | None = None
    album: str | None = None
    index: int = 0
    count: int = 0
    position: float = 0.0
    duration: float | None = None
    art: dict[str, str] | None = None
    idle: bool = True
    url: str | None = None
    #: The playlist these entries came from, so the app can name what is playing. ``None`` when
    #: mpv was handed loose files rather than a playlist.
    source_url: str | None = None
    source_title: str | None = None

    def to_message(self) -> dict[str, Any]:
        return {
            "type": SERVER_STATE,
            "playing": self.playing,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "index": self.index,
            "count": self.count,
            "position": round(self.position, 3),
            "duration": round(self.duration, 3) if self.duration is not None else None,
            "art": self.art,
            "idle": self.idle,
            "url": self.url,
            "source": (
                {"url": self.source_url, "title": self.source_title}
                if self.source_url
                else None
            ),
        }


@dataclass(slots=True)
class PlaylistEntry:
    index: int
    title: str
    current: bool = False
    url: str | None = None
    #: True when mpv gave us no real title -- a deleted, private or region-blocked video. The app
    #: hides these by default.
    unavailable: bool = False

    def to_message(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "title": self.title,
            "current": self.current,
            "url": self.url,
            "unavailable": self.unavailable,
        }


@dataclass(slots=True)
class Playlist:
    entries: list[PlaylistEntry] = field(default_factory=list)

    def to_message(self) -> dict[str, Any]:
        return {
            "type": SERVER_PLAYLIST,
            "entries": [entry.to_message() for entry in self.entries],
        }


@dataclass(slots=True)
class SavedPlaylist:
    """One playlist the daemon has cached, and can start playing without re-resolving it."""

    url: str
    title: str | None = None
    count: int = 0
    fetched: int = 0
    current: bool = False

    def to_message(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "count": self.count,
            "fetched": self.fetched,
            "current": self.current,
        }


def library_message(playlists: list[SavedPlaylist]) -> dict[str, Any]:
    return {
        "type": SERVER_LIBRARY,
        "playlists": [playlist.to_message() for playlist in playlists],
    }


def hello_message(mpv_version: str | None) -> dict[str, Any]:
    return {
        "type": SERVER_HELLO,
        "version": PROTOCOL_VERSION,
        "mpv": mpv_version,
    }


def error_message(reason: str) -> dict[str, Any]:
    return {"type": SERVER_ERROR, "reason": reason}


#: Which job a ``refresh`` message is reporting on. They share a message type because from the
#: app's side they are the same wait, but "added 855 tracks" and "found 855 tracks" are not the
#: same sentence, so the app has to be able to tell them apart.
REFRESH_KIND_RECHECK = "refresh"
REFRESH_KIND_ADD = "add"
REFRESH_KIND_LOAD = "load"


def refresh_message(
    status: str,
    added: int = 0,
    total: int = 0,
    reason: str | None = None,
    title: str | None = None,
    url: str | None = None,
    kind: str = REFRESH_KIND_RECHECK,
) -> dict[str, Any]:
    """Progress for a playlist fetch; yt-dlp on a large playlist takes many seconds.

    Re-checking the current playlist, adding a new one and loading an unseen one all report
    through this. ``title`` and ``url`` name the playlist; ``kind`` names the job.
    """
    return {
        "type": SERVER_REFRESH,
        "status": status,
        "added": added,
        "total": total,
        "reason": reason,
        "title": title,
        "url": url,
        "kind": kind,
    }
