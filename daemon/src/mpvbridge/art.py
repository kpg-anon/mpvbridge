"""Album art resolution.

Two sources, in order of preference:

* A YouTube video id lifted out of the current ``path`` becomes an ``i.ytimg.com`` URL that the
  app fetches directly. No extra yt-dlp call, so track changes stay instant.
* A local file gets a sidecar cover (``cover.jpg`` and friends) inlined as base64.

Art embedded inside a local file's tags is not read; that needs ffmpeg or mutagen and is a
documented gap.
"""

from __future__ import annotations

import base64
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

MAX_INLINE_ART_BYTES = 1_000_000

_YOUTUBE_ID = r"(?P<id>[0-9A-Za-z_-]{11})"
_YOUTUBE_PATTERNS = (
    re.compile(r"^https?://(?:www\.|m\.|music\.)?youtube\.com/watch\?(?:[^#]*&)?v=" + _YOUTUBE_ID),
    re.compile(r"^https?://(?:www\.)?youtu\.be/" + _YOUTUBE_ID),
    re.compile(r"^https?://(?:www\.)?youtube\.com/(?:embed|v|shorts|live)/" + _YOUTUBE_ID),
    re.compile(r"^ytdl://" + _YOUTUBE_ID + r"$"),
)

_SIDECAR_NAMES = ("cover", "folder", "front", "album", "albumart")
_SIDECAR_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")

_MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


def youtube_id(path: str | None) -> str | None:
    """Return the 11-character video id in *path*, or None."""
    if not path:
        return None
    for pattern in _YOUTUBE_PATTERNS:
        match = pattern.match(path)
        if match:
            return match.group("id")
    return None


def youtube_thumbnail_url(video_id: str) -> str:
    # maxresdefault is missing for plenty of videos; the app falls back to hqdefault, which
    # always exists.
    return f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"


def youtube_thumbnail_fallback_url(video_id: str) -> str:
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"


def sidecar_cover(path: str | None) -> Path | None:
    """Find a cover image sitting next to a local media file."""
    if not path:
        return None
    try:
        media = Path(path)
        if not media.is_file():
            return None
    except OSError:
        return None

    candidates: list[Path] = []
    for suffix in _SIDECAR_SUFFIXES:
        candidates.append(media.with_suffix(suffix))
    for name in _SIDECAR_NAMES:
        for suffix in _SIDECAR_SUFFIXES:
            candidates.append(media.parent / f"{name}{suffix}")

    for candidate in candidates:
        try:
            if candidate.is_file() and candidate.stat().st_size <= MAX_INLINE_ART_BYTES:
                return candidate
        except OSError:
            continue
    return None


def resolve(path: str | None) -> dict[str, str] | None:
    """Build the ``art`` field of a state message for the currently playing *path*."""
    video_id = youtube_id(path)
    if video_id is not None:
        return {
            "url": youtube_thumbnail_url(video_id),
            "fallbackUrl": youtube_thumbnail_fallback_url(video_id),
        }

    cover = sidecar_cover(path)
    if cover is not None:
        try:
            raw = cover.read_bytes()
        except OSError as exc:
            log.debug("could not read cover %s: %s", cover, exc)
            return None
        mime = _MIME_BY_SUFFIX.get(cover.suffix.lower(), "image/jpeg")
        return {"data": base64.b64encode(raw).decode("ascii"), "mime": mime}

    return None
