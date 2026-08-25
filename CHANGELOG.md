# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning is `MAJOR.MINOR.PATCH` and **0.x means beta** — a minor bump may change the wire
protocol or move settings around, a patch bump never does. The app and the daemon ship as one
version and are expected to match.

## [Unreleased]

Nothing yet.

## [0.1.0] - 2026-08-25

First release. Both halves — the Termux daemon and the Android companion app.

### Added

- **Real media controls for mpv.** A `MediaSession` in a companion app, proxied to mpv over its
  JSON IPC socket. Lockscreen card, notification transport, album art, and Bluetooth headset
  buttons — the last of which Termux:API cannot do at all, because its media notification never
  calls `setMediaSession`.
- **`SilentAudio` keep-alive.** Android picks the media-button session by walking the UIDs that are
  currently producing audio; mpv's audio belongs to Termux's UID, which owns no session. Looping one
  second of silence at zero volume puts the app in that list and claims the routing. Without it,
  `dumpsys media_session` reports `Media button session is null` and headset taps reach nothing.
- **The app is the way in.** `mpvbridge` with no media argument starts mpv idle and waits to be told
  what to play. Opening the app starts the daemon in Termux, waits for the socket, and loads the
  playlist that was playing last — about six seconds from icon tap to audio, with nothing typed in a
  terminal.
- **Playlist library.** Add a YouTube playlist by pasting a link or sharing one to the app from
  YouTube. yt-dlp names it and the daemon caches it; tapping it later starts playback with no
  network round trip. Long-press to forget one.
- **Search** on the Playlist and Favorites screens, matching on title and artist. Rows keep mpv's
  own index, so tapping a filtered row still jumps to the right track.
- **Fast startup.** Handing mpv a playlist URL makes it run a full `yt-dlp --flat-playlist` fetch
  before playback starts, on every launch. The daemon caches resolved ids and titles under
  `$XDG_CACHE_HOME/mpvbridge` and hands mpv a local playlist file instead.
- **Unavailable-track handling.** Deleted, private and region-blocked videos resolve no title and
  would otherwise show as bare URLs — 40 of 855 in the playlist this was built against. They are
  flagged and hidden by default; the currently playing entry is never hidden. The rule applies only
  to `http(s)` entries, so a local library does not vanish.
- **Check for new tracks**, on demand from the Playlist screen or automatically on launch.
- **Favorites**, with export and import through the system file picker.
- **Shuffle and unshuffle** as buttons rather than a launch flag. A shuffle is not written back to
  the cache, so the next launch still starts from the source order and *Unshuffle* still has
  something to undo.
- **Documented wire protocol** — newline-delimited JSON over loopback TCP, in
  [`PROTOCOL.md`](PROTOCOL.md), with an optional shared-secret handshake.
- **Development pipeline.** `tools/dev.sh` builds, installs and deploys from a PC over wireless adb;
  `tools/agent.sh` runs inside Termux and picks up deploys, because adb runs as `shell` and cannot
  reach Termux's home.
- 111 daemon tests.

### Known gaps

- Embedded cover art inside local files is not read; only sidecar `cover.jpg` and YouTube
  thumbnails. Reading art out of tags needs ffmpeg or mutagen.
- Favorites live in app-private storage, so uninstalling loses them. Export first.
- The app shows an empty list for the second or so before the socket connects — the daemon caches,
  the app does not.

[Unreleased]: https://github.com/kpg-anon/mpvbridge/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/kpg-anon/mpvbridge/releases/tag/v0.1.0
