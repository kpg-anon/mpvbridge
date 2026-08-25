<div align="center">

<img src="https://raw.githubusercontent.com/kpg-anon/mpvbridge/main/.github/assets/hero.svg" alt="mpvbridge — native Android media controls for mpv running in Termux" width="880">

<p>
  <a href="https://github.com/kpg-anon/mpvbridge/releases"><img src="https://img.shields.io/github/v/release/kpg-anon/mpvbridge?display_name=tag&sort=semver&logo=github&label=release&color=FF1FD0" alt="Latest release"></a>
  <a href="https://github.com/kpg-anon/mpvbridge/blob/main/CHANGELOG.md"><img src="https://img.shields.io/badge/changelog-keep%20a%20changelog-25F4EE" alt="Changelog"></a>
  <a href="https://github.com/kpg-anon/mpvbridge/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/kpg-anon/mpvbridge/ci.yml?branch=main&logo=githubactions&logoColor=white&label=CI" alt="CI"></a>
  <img src="https://img.shields.io/badge/kotlin-2.4-7F52FF?logo=kotlin&logoColor=white" alt="Kotlin 2.4">
  <img src="https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/Android-8.0%2B%20%C2%B7%20API%2026-3DDC84?logo=android&logoColor=white" alt="Android 8.0+">
  <img src="https://img.shields.io/badge/license-MIT-blue" alt="MIT license">
</p>

<p><b>Lockscreen card, notification transport, album art, and Bluetooth headset buttons that actually reach <a href="https://mpv.io/">mpv</a>.</b></p>

</div>

---

## What it is

You already listen with `mpv` and `yt-dlp` in a terminal. mpvbridge makes the phone treat that like
a real music app — and makes the terminal optional.

Open the app and it plays. It starts the daemon in Termux itself, waits for the socket, and loads
whatever you were playing last. Measured cold, icon tap to first audio is about six seconds.

<p align="center">
  <img src="https://raw.githubusercontent.com/kpg-anon/mpvbridge/main/screenshots/nowplaying.png" alt="Now Playing: hero artwork, title, seek bar and transport" width="260">
  <img src="https://raw.githubusercontent.com/kpg-anon/mpvbridge/main/screenshots/library.png" alt="Library: saved playlists named by yt-dlp" width="260">
  <img src="https://raw.githubusercontent.com/kpg-anon/mpvbridge/main/screenshots/playlist.png" alt="Playlist: 855 tracks with a search box narrowing to four" width="260">
</p>

```
Termux                                    Android
┌────────────────────────┐                ┌──────────────────────────────┐
│ mpv ──JSON IPC socket──┼── mpvbridge ───┼─▶ MpvSessionService          │
│                        │   :7355 TCP    │   ├─ MediaSession  ◀── AVRCP │
│ yt-dlp                 │  (loopback)    │   ├─ media notification      │
└────────────────────────┘                │   └─ SilentAudio keep-alive  │
                                          └──────────────────────────────┘
```

> [!NOTE]
> This is 0.x, beta software. It works, and it is what I listen to every day on a Galaxy S24+ —
> but the wire protocol and the settings layout may still change between minor versions. See
> [Versioning](#versioning).

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

<details>
<summary><b>The keep-alive, and why it is not optional</b></summary>

<br>

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
Media button session is io.github.kpganon.mpvbridge/androidx.media3.session.id.mpv
```

Turning it off in Settings disables headset controls entirely. That is the whole mechanism.

</details>

## Requirements

- Termux from **F-Droid or GitHub** — the Play Store build is deprecated and unmaintained
- `pkg install python mpv yt-dlp termux-am`
- Python 3.11+, mpv 0.38+
- Android 8.0+ (developed against Android 16 / One UI 8)

## Install

**1. The app.** Download the APK from [Releases](https://github.com/kpg-anon/mpvbridge/releases)
and install it. Android will ask you to allow installing from your browser or file manager.

**2. The daemon**, in Termux:

```console
pip install https://github.com/kpg-anon/mpvbridge/releases/latest/download/mpvbridge-py.tar.gz
```

<details>
<summary><b>From source</b> — clone and install both halves</summary>

<br>

```console
git clone https://github.com/kpg-anon/mpvbridge.git
cd mpvbridge/daemon && pip install .
```

The app:

```console
cd mpvbridge/android-app && ./gradlew :app:assembleDebug
```

The APK lands in `android-app/app/build/outputs/apk/debug/`. Needs JDK 17.

</details>

**3. Let Termux take orders.** Two separate things have to be true, and each fails silently on its
own.

Tell Termux to accept commands from other apps:

```console
mkdir -p ~/.termux
sed -i '/allow-external-apps/d' ~/.termux/termux.properties 2>/dev/null
echo 'allow-external-apps=true' >> ~/.termux/termux.properties
termux-reload-settings
```

Then open mpvbridge and answer **Allow** when it asks to run commands in Termux.
`com.termux.permission.RUN_COMMAND` is declared by Termux as a *dangerous* permission, so listing
it in the app's manifest is not enough — Android will not grant it at install time. If you decline,
Settings → *Start playback in Termux* asks again.

> [!TIP]
> If the app says "Termux was asked to start, but nothing is listening", it is the
> `allow-external-apps` line. Termux refuses the command and reports it in a notification of its
> own, which the app cannot see.

## Use

Open the app. Tap **+** on the Library screen and paste a YouTube playlist link — or share one to
mpvbridge straight from the YouTube app. yt-dlp names it, the daemon caches it, and it appears in
the Library. Tap it to play.

| Screen | What's there |
| --- | --- |
| **Now Playing** | hero artwork, title, artist, seek bar, transport, star, shuffle |
| **Library** | every cached playlist, named by yt-dlp. Tap to play, long-press to forget, **+** to add |
| **Playlist** | every entry, numbered by mpv's own index, current row highlighted, tap to jump, search box |
| **Favorites** | starred tracks, tap to play, long-press to remove, export/import via the file picker |
| **Settings** | connection, playlist and playback behaviour, launching, favorites, about |

A mini-player sits on every screen, and the standard notification and lockscreen card work the way
they do for any music app.

<details>
<summary><b>Starting it by hand</b> — mpvbridge is still a drop-in mpv wrapper</summary>

<br>

Anything `mpvbridge` does not recognise is passed straight through to mpv:

```console
mpvbridge --vo=null 'https://www.youtube.com/playlist?list=...'
```

Quote the URL **or** escape the `?` and `=`, not both — inside quotes a backslash is a literal
character and mpv will request a path that does not exist.

`--no-video` instead of `--vo=null` skips video decoding entirely rather than decoding and throwing
frames away: real battery and data savings on a music playlist.

Given no media argument at all, mpv starts idle and waits for the app to say what to play. That is
what the app's default launch command (`mpvbridge --vo=null`) does.

You do not need `--shuffle`. The playlist order is stable across launches, and shuffling is a button
in the app. A shuffle is not written back to the cache, so the next launch still starts from the
source order and *Unshuffle* still has something to undo.

### Bridge options

Prefixed to stay clear of mpv's own several hundred options.

| option | meaning |
| --- | --- |
| `--bridge-port N` | TCP port, default 7355 |
| `--bridge-host ADDR` | bind address, default 127.0.0.1 |
| `--bridge-token SECRET` | require this token in the app's handshake |
| `--bridge-attach SOCKET` | attach to an mpv already running with `--input-ipc-server=SOCKET` |
| `--bridge-no-cache` | always let mpv resolve the playlist URL instead of using the cached copy |
| `--bridge-idle` | start mpv with nothing loaded and wait for the app (implied by no media argument) |
| `--bridge-no-launch` | do not bring the app to the foreground |
| `--bridge-no-wake-lock` | do not acquire a Termux wake lock |
| `--bridge-verbose` | log bridge activity to stderr |
| `--bridge-help` | this list |

</details>

## How the fast parts work

<details>
<summary><b>Naming a playlist costs no extra fetch</b></summary>

<br>

The name is not guessed from the URL. `yt-dlp --flat-playlist` reports the playlist's own title on
every entry it prints, so the same call that fills the cache also names the playlist — one fetch,
not two. Until that fetch finishes the row shows the URL.

Because adding a playlist caches it, playing it later starts immediately with no yt-dlp call at all.

</details>

<details>
<summary><b>Startup does not wait on yt-dlp</b></summary>

<br>

Handing mpv a YouTube playlist URL makes it run a full `yt-dlp --flat-playlist` fetch before
playback starts — many seconds for a large playlist, on **every** launch. The daemon caches the
resolved video ids and titles under `$XDG_CACHE_HOME/mpvbridge`, then hands mpv a local playlist
file instead. mpv still resolves each video as it reaches it, so playback begins immediately.

Because mpv then knows no titles up front, the daemon overlays cached ones — without that, every row
would look like a dead bare URL.

Use **Check for new tracks** on the Playlist screen when you have added songs to the source
playlist. Settings can do it automatically on launch.

Switching playlists in the app is the same mechanism: mpv is handed the cached local file for the
playlist you picked, so it starts playing without waiting on the network.

</details>

<details>
<summary><b>Deleted and private videos are flagged, not shown as bare URLs</b></summary>

<br>

A long YouTube playlist accumulates deleted, private and region-blocked videos. mpv resolves no
title for those, so they show as bare URLs — 40 of 855 in the playlist this was built against. The
daemon flags them and the app hides them by default (Settings → *Hide unavailable tracks*). The
currently playing entry is never hidden, however broken its metadata looks.

A **local** file legitimately has no title, so the missing-title rule only applies to `http(s)`
entries — otherwise an entire local library would vanish.

</details>

## Keeping it alive on Samsung

One UI is aggressive about background apps. If playback dies when the screen goes off:

- Settings → Apps → **Termux** → Battery → **Unrestricted**
- Same for the **mpvbridge** app
- Settings → Battery → Background usage limits → make sure neither app is in *Sleeping apps* or
  *Deep sleeping apps*

The daemon takes a `termux-wake-lock` automatically (disable with `--bridge-no-wake-lock`).

## Troubleshooting

| symptom | cause |
| --- | --- |
| Headset buttons do nothing | Keep-alive is off, or another app grabbed the session. Check `adb shell dumpsys media_session \| grep "Media button session"` — it must name this app. |
| App says "Waiting for mpvbridge" | The daemon is not running, or the port in Settings does not match `--bridge-port`. |
| App says "Connected · nothing loaded" | The daemon is up and mpv is idle. Pick a playlist from Library. |
| Opening the app does not start Termux | Settings → *Start Termux when the app opens* is off, or the `com.termux.permission.RUN_COMMAND` prompt was declined. Tap Settings → *Start playback in Termux* to be asked again. |
| "Termux was asked to start, but nothing is listening" | `allow-external-apps=true` is missing from `~/.termux/termux.properties`. Termux refuses the command and reports it in its own notification. |
| Title and artwork frozen while next/prev still work | Writes reach mpv but property updates do not — the IPC reader died. Check the daemon's stderr. |
| Two songs at once | An orphaned mpv from an earlier daemon. `pkill mpv`. |
| yt-dlp 404 on a playlist URL | The URL is both quoted *and* backslash-escaped. Use one or the other. |

## Versioning

`MAJOR.MINOR.PATCH`, and **0.x means beta**: the app and the daemon ship as one version and are
expected to match. A minor bump may change the wire protocol or move settings around; a patch bump
never does.

The two halves keep their versions in one place each — `android-app/gradle.properties` and
`daemon/src/mpvbridge/__init__.py` — and the release workflow refuses to publish unless both equal
the tag. `versionCode` is derived from the version rather than tracked by hand.

Every change is in [`CHANGELOG.md`](CHANGELOG.md). Releasing is documented in
[`RELEASING.md`](RELEASING.md).

## Development

```console
tools/dev.sh up        # build, install, deploy the daemon, stream both logs
tools/dev.sh connect   # re-establish wireless adb after it drops
tools/dev.sh status    # device, agent, daemon, media-button session at a glance
```

`tools/agent.sh` runs once inside Termux and watches shared storage for a deploy trigger, because
the PC cannot run a command in Termux directly: adb is the `shell` user and cannot read Termux's
home, and Termux's `RunCommandService` is guarded by a permission `pm grant` cannot give to
`com.android.shell` — it only flips permissions a package already declares. The app itself *can*
hold that permission, which is what makes the Start button in Settings work.

Tests:

```console
cd daemon && PYTHONPATH=src python -m pytest
```

The wire protocol is documented in [`PROTOCOL.md`](PROTOCOL.md).

## Credits

Inspired by [Neo-Oli/Termux-Mpv](https://github.com/Neo-Oli/Termux-Mpv), which pioneered
mpv-with-a-notification on Termux. No code is shared; this was written from scratch around a real
`MediaSession`, which is what the original could not do.

## Licence

MIT — see [LICENSE](LICENSE).
