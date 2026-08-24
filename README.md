# termux-mpv-controls

Native Android media controls for mpv running in Termux — lockscreen card, notification transport,
album art, and **Bluetooth headset buttons that actually reach mpv**.

You keep listening the way you already do (`mpv` + `yt-dlp` in a terminal), and the phone treats it
like a real music app.

```
Termux                                    Android
┌────────────────────────┐                ┌──────────────────────────────┐
│ mpv ──JSON IPC socket──┼── mpvbridge ───┼─▶ MpvSessionService          │
│                        │   :7355 TCP    │   ├─ MediaSession  ◀── AVRCP │
│ yt-dlp                 │  (loopback)    │   ├─ media notification      │
└────────────────────────┘                │   └─ SilentAudio keep-alive  │
                                          └──────────────────────────────┘
```

## Why this exists

The obvious approach — Termux:API's `termux-notification --type media` — cannot do headset buttons,
and it is worth being precise about why.

`NotificationAPI.java` builds that notification with
`androidx.media.app.NotificationCompat.MediaStyle()` and **never calls `.setMediaSession(token)`**.
There is no `MediaSession` anywhere in Termux:API. Android routes Bluetooth AVRCP key events — what
a bud double-tap actually sends — to the highest-priority active `MediaSession`. With no session,
those taps reach nothing. The prev/play/next buttons in that notification are ordinary notification
actions that shell out through `am`; they only respond to a finger.

So this project runs a real `MediaSession` in a companion app and proxies it to mpv.

### The keep-alive, and why it is not optional

A `MediaSession` alone is still not enough. AOSP's
`MediaSessionStack.updateMediaButtonSessionIfNeeded()` picks the media-button session by walking the
UIDs that are **currently producing audio** and taking the first one that owns a session. mpv's
audio belongs to Termux's UID, which owns no session; this app owns a session but produces no audio.
Measured on a Galaxy S24+ (One UI 8.0.5, Android 16), the result is:

```
Media button session is null
```

`SilentAudio` fixes it by playing one second of silence on a loop (`MODE_STATIC`, zero volume, no
audio-focus request). That puts the app's UID into the playback list the router walks, and the
session is claimed:

```
Media button session is io.github.kpganon.termuxmpvcontrols/androidx.media3.session.id.mpv
```

Turning it off in Settings disables headset controls entirely. That is the whole mechanism.

## Requirements

- Termux from **F-Droid or GitHub** — the Play Store build is deprecated and unmaintained
- `pkg install python mpv yt-dlp termux-am`
- Python 3.11+, mpv 0.38+
- Android 8.0+ (developed against Android 16 / One UI 8)

## Install

**Daemon**, in Termux:

```sh
git clone https://github.com/kpg-anon/termux-mpv-controls
cd termux-mpv-controls/daemon && pip install .
```

**App**: install the APK from [Releases](../../releases), or build it with
`cd android-app && ./gradlew :app:assembleDebug`.

## Use

`mpvbridge` is a drop-in replacement for `mpv` — anything it does not recognise is passed straight
through:

```sh
mpvbridge --vo=null 'https://www.youtube.com/playlist?list=...'
```

Quote the URL **or** escape the `?` and `=`, not both — inside quotes a backslash is a literal
character and mpv will request a path that does not exist.

`--no-video` instead of `--vo=null` skips video decoding entirely rather than decoding and throwing
frames away: real battery and data savings on a music playlist.

You do not need `--shuffle`. The playlist order is stable across launches, and shuffling is a button
in the app.

### Bridge options

Prefixed to stay clear of mpv's own several hundred options.

| option | meaning |
| --- | --- |
| `--bridge-port N` | TCP port, default 7355 |
| `--bridge-host ADDR` | bind address, default 127.0.0.1 |
| `--bridge-token SECRET` | require this token in the app's handshake |
| `--bridge-attach SOCKET` | attach to an mpv already running with `--input-ipc-server=SOCKET` |
| `--bridge-no-cache` | always let mpv resolve the playlist URL instead of using the cached copy |
| `--bridge-no-launch` | do not bring the app to the foreground |
| `--bridge-no-wake-lock` | do not acquire a Termux wake lock |
| `--bridge-verbose` | log bridge activity to stderr |
| `--bridge-help` | this list |

## What the app does

- **Now Playing** — hero artwork, title, artist, seek bar, transport, star, shuffle
- **Playlist** — every entry, numbered by mpv's own index, current row highlighted, tap to jump
- **Favorites** — starred tracks, tap to play, long-press to remove, export/import via the system
  file picker
- **Settings** — connection, playlist and playback behaviour, launching, favorites, about
- **Mini-player** on every screen, plus the standard notification and lockscreen card

### Fast startup

Handing mpv a YouTube playlist URL makes it run a full `yt-dlp --flat-playlist` fetch before
playback starts — many seconds for a large playlist, on **every** launch. The daemon caches the
resolved video ids and titles under `$XDG_CACHE_HOME/mpvbridge`, then hands mpv a local playlist
file instead. mpv still resolves each video as it reaches it, so playback begins immediately.

Because mpv then knows no titles up front, the daemon overlays cached ones — without that, every row
would look like a dead bare URL.

Use **Check for new tracks** on the Playlist screen when you have added songs to the source
playlist. Settings can do it automatically on launch.

### Unavailable tracks

A long YouTube playlist accumulates deleted, private and region-blocked videos. mpv resolves no
title for those, so they show as bare URLs — 40 of 855 in the playlist this was built against. The
daemon flags them and the app hides them by default (Settings → *Hide unavailable tracks*). The
currently playing entry is never hidden, however broken its metadata looks.

A **local** file legitimately has no title, so the missing-title rule only applies to `http(s)`
entries — otherwise an entire local library would vanish.

## Keeping it alive on Samsung

One UI is aggressive about background apps. If playback dies when the screen goes off:

- Settings → Apps → **Termux** → Battery → **Unrestricted**
- Same for **Termux mpv Controls**
- Settings → Battery → Background usage limits → make sure neither app is in *Sleeping apps* or
  *Deep sleeping apps*

The daemon takes a `termux-wake-lock` automatically (disable with `--bridge-no-wake-lock`).

## Troubleshooting

| symptom | cause |
| --- | --- |
| Headset buttons do nothing | Keep-alive is off, or another app grabbed the session. Check `adb shell dumpsys media_session \| grep "Media button session"` — it must name this app. |
| App says "Waiting for mpvbridge" | The daemon is not running, or the port in Settings does not match `--bridge-port`. |
| Title and artwork frozen while next/prev still work | Writes reach mpv but property updates do not — the IPC reader died. Check the daemon's stderr. |
| Two songs at once | An orphaned mpv from an earlier daemon. `pkill mpv`. |
| yt-dlp 404 on a playlist URL | The URL is both quoted *and* backslash-escaped. Use one or the other. |

## Development

```sh
tools/dev.sh up        # build, install, deploy the daemon, stream both logs
tools/dev.sh connect   # re-establish wireless adb after it drops
tools/dev.sh status    # device, agent, daemon, media-button session at a glance
```

`tools/agent.sh` runs once inside Termux and watches shared storage for a deploy trigger, because
the PC cannot run a command in Termux directly: adb is the `shell` user and cannot read Termux's
home, and Termux's `RunCommandService` is guarded by a permission `pm grant` cannot give to
`com.android.shell` — it only flips permissions a package already declares. The app itself *can*
hold that permission, which is what makes the Start button in Settings work.

Tests: `cd daemon && PYTHONPATH=src python -m pytest`

## Protocol

[`PROTOCOL.md`](PROTOCOL.md).

## Credits

Inspired by [Neo-Oli/Termux-Mpv](https://github.com/Neo-Oli/Termux-Mpv), which pioneered
mpv-with-a-notification on Termux. No code is shared; this was written from scratch around a real
`MediaSession`, which is what the original could not do.

## Licence

MIT — see [LICENSE](LICENSE).
