# mpvbridge

The Termux half of the mpv media session setup. It runs mpv, watches its JSON IPC socket, and
publishes what it sees on a loopback TCP socket that the companion Android app connects to.

It has no dependencies beyond the Python standard library.

## Install

```sh
pkg install python mpv yt-dlp termux-am
pip install .
```

`termux-am` supplies the `am` command, which the bridge uses to bring the app to the foreground
when playback starts. Everything still works without it; you just have to open the app yourself.

## Use

`mpvbridge` is a drop-in replacement for `mpv`. Anything it does not recognise is passed straight
through:

```sh
mpvbridge --shuffle --vo=null 'https://www.youtube.com/playlist?list=...'
```

Quote the URL, or escape the `?` and `=` — but not both. Inside quotes a backslash is a literal
character and mpv will ask YouTube for a path that does not exist.

`--no-video` instead of `--vo=null` skips video decoding altogether rather than decoding and
discarding it, which is worth real battery and data on a music playlist.

### Bridge options

Every option below is prefixed to keep it clear of mpv's own, which number in the hundreds.

| option | meaning |
| --- | --- |
| `--bridge-port N` | TCP port, default 7355 |
| `--bridge-host ADDR` | bind address, default 127.0.0.1 |
| `--bridge-token SECRET` | require this token in the app's handshake |
| `--bridge-attach SOCKET` | attach to an mpv already running with `--input-ipc-server=SOCKET` |
| `--bridge-no-launch` | do not try to foreground the companion app |
| `--bridge-no-wake-lock` | do not acquire a Termux wake lock |
| `--bridge-verbose` | log bridge activity to stderr |
| `--bridge-help` | this list |

## How it talks to mpv

Commands are real mpv commands — `playlist-next weak`, `set_property pause`, `seek … absolute` —
rather than simulated keypresses, so they do not depend on your `input.conf` and work when mpv has
no terminal focus.

The track title comes from mpv's `media-title`, which already resolves tag title → yt-dlp title →
filename, so it is correct for YouTube playlists, internet radio and local files alike.

Position is not sent on a timer. The app extrapolates its seek bar between the anchors the bridge
sends on pause, seek and track change, so the socket is idle during normal playback.

## Security

The server binds loopback only, so it is not reachable from the network — but any app on the phone
can connect to it and control mpv. Use `--bridge-token` if that matters to you.

## Tests

```sh
pip install pytest
PYTHONPATH=src python -m pytest
```

## Protocol

See [`../PROTOCOL.md`](../PROTOCOL.md).
