# Bridge protocol

`mpvbridge` (Termux) listens on `127.0.0.1:7355`. The companion app connects to it. Both
directions are **newline-delimited JSON**, UTF-8, one object per line. Every object has a `type`.

The socket is loopback-only, so it is not reachable from the network — but any app on the phone
can connect to it. Pass `--bridge-token` if that matters to you.

## Server → client

### `hello`

Sent as soon as a client is accepted (or, with a token configured, once its handshake succeeds).

```json
{"type": "hello", "version": 1, "mpv": "mpv 0.38.0"}
```

### `state`

The full snapshot. Sent on connect and whenever anything below changes. Not sent for position
alone — the app extrapolates the seek bar from the last `position`/`playing` pair, so the socket
stays quiet during playback.

```json
{
  "type": "state",
  "playing": true,
  "title": "Song Title",
  "artist": "Artist",
  "album": "Album",
  "index": 3,
  "count": 855,
  "position": 12.42,
  "duration": 214.6,
  "art": {"url": "https://i.ytimg.com/vi/<id>/maxresdefault.jpg",
          "fallbackUrl": "https://i.ytimg.com/vi/<id>/hqdefault.jpg"},
  "idle": false,
  "url": "https://www.youtube.com/watch?v=<id>",
  "source": {"url": "https://www.youtube.com/playlist?list=<id>", "title": "share"}
}
```

`url` is mpv's `path` for the current entry. The app needs it to star a track and replay it later.

`source` is the playlist these entries came from, and is `null` when mpv was handed loose files.
mpv rewrites `playlist-path` the moment a playlist is expanded, so the daemon carries the original
alongside rather than reading it back out of mpv.

`artist`, `album`, `duration` and `art` may be `null`. `art` is either the URL pair above or
inline bytes for a local sidecar cover:

```json
{"art": {"data": "<base64>", "mime": "image/jpeg"}}
```

`title` comes from mpv's `media-title`, which already resolves tag title → yt-dlp title →
filename, so it is correct for YouTube playlists, streams and local files alike.

### `playlist`

Sent on connect and whenever `playlist-count` or `playlist-pos` changes.

```json
{"type": "playlist",
 "entries": [{"index": 0, "title": "First", "current": false,
              "url": "https://www.youtube.com/watch?v=<id>", "unavailable": false}]}
```

`unavailable` marks an entry mpv could resolve no title for — a deleted, private or region-blocked
video, which otherwise shows as a bare URL. A **local** file with no title is not flagged; the rule
applies only to `http(s)` entries. The app hides flagged entries by default but never hides the
entry that is currently playing.

`current` is informational. The authoritative current entry is `index` in the `state` message.

### `library`

Every playlist the daemon has cached, newest fetch first. Sent on connect, after `add-playlist`,
`load-playlist` and `remove-playlist`, and on request.

```json
{"type": "library",
 "playlists": [{"url": "https://www.youtube.com/playlist?list=<id>",
                "title": "share", "count": 855, "fetched": 1755000000, "current": true}]}
```

`title` is what yt-dlp reported for the playlist, or `null` if it never has been fetched. `count`
is how many entries the cache holds. `current` marks the one mpv is playing.

This is the app's Library screen. Because every entry is already cached, tapping one starts
playing it without a yt-dlp round trip.

### `bye`

mpv exited; the bridge is shutting down.

```json
{"type": "bye"}
```

### `refresh`

Progress of anything that shells out to yt-dlp — `refresh-playlist`, `add-playlist`, and a
`load-playlist` for a playlist that has never been fetched. They are one message type because from
the app's side they are the same wait, and `yt-dlp` on a large playlist takes many seconds.

```json
{"type": "refresh", "status": "running", "added": 0, "total": 0, "reason": null,
 "title": "share", "url": "https://www.youtube.com/playlist?list=<id>"}
{"type": "refresh", "status": "done", "added": 3, "total": 858, "reason": null,
 "title": "share", "url": "https://www.youtube.com/playlist?list=<id>"}
{"type": "refresh", "status": "error", "added": 0, "total": 0, "reason": "yt-dlp timed out",
 "title": null, "url": "https://www.youtube.com/playlist?list=<id>"}
```

`title` and `url` name which playlist the progress is about. `title` is `null` until yt-dlp has
reported one.

### `error`

A client message was rejected. The connection stays open unless the reason is fatal
(`bad token`, `line too long`).

```json
{"type": "error", "reason": "unknown command: 'self-destruct'"}
```

## Client → server

### `hello`

Only required when the bridge was started with `--bridge-token`. Nothing else is accepted until
it succeeds.

```json
{"type": "hello", "token": "s3cret"}
```

### `cmd`

```json
{"type": "cmd", "name": "next"}
```

| `name`     | extra field         | mpv command                          |
| ---------- | ------------------- | ------------------------------------ |
| `play`     | —                   | `set_property pause false`           |
| `pause`    | —                   | `set_property pause true`            |
| `toggle`   | —                   | `cycle pause`                        |
| `next`     | —                   | `playlist-next weak`                 |
| `previous` | —                   | `playlist-prev weak`                 |
| `stop`     | —                   | `set_property pause true`            |
| `seek`     | `position`: seconds | `seek <position> absolute`           |
| `goto`     | `index`: int        | `set_property playlist-pos <index>`  |
| `refresh`  | —                   | none; re-sends `state` and `playlist` |
| `shuffle`  | —                   | `playlist-shuffle`                   |
| `unshuffle` | —                  | `playlist-unshuffle`                 |
| `play-url` | `url`: string       | `loadfile <url> insert-next-play`    |
| `refresh-playlist` | —           | re-reads the source with yt-dlp; see below |
| `library`  | —                   | none; re-sends `library`             |
| `add-playlist` | `url`: string   | none; resolves and caches it, then re-sends `library` |
| `load-playlist` | `url`: string  | `loadlist <cached file> replace`     |
| `remove-playlist` | `url`: string | none; drops it from the cache       |

These are real mpv commands rather than simulated keypresses, so they do not depend on the user's
`input.conf` and work when mpv has no terminal focus.

`stop` is deliberately a pause: dismissing a notification should not throw away a loaded playlist.

`play-url` deliberately inserts after the current entry rather than using `replace`, which would
discard the loaded playlist. `insert-next-play` is newer than some mpv builds, so the daemon falls
back to `append-play` if mpv rejects it.

`unshuffle` only restores order for a shuffle mpv performed itself. A playlist started with
`--shuffle` has no original order to return to — which is why the recommended launch command omits
that flag and leaves shuffling to the app.

`refresh-playlist` runs `yt-dlp --flat-playlist` against the original playlist URL, updates the
cache, appends anything mpv does not already have, and reports progress through `refresh` messages.
The source URL is remembered at startup because mpv rewrites `playlist-path` once a playlist is
expanded.

`add-playlist` runs the same fetch against a URL the daemon has never seen. The playlist's own name
rides along on every entry of `--flat-playlist` output, so naming it costs no extra call — which is
how a pasted link becomes "share" in the app.

`load-playlist` switches mpv to a playlist in the library. Unlike `play-url` it *does* use
`replace`, because it is a deliberate "play this instead" rather than a one-off insert. A playlist
already in the cache is loaded from its local file with no yt-dlp call at all; one that is not is
resolved first. It also clears `pause`, since `stop` from the notification is a pause and a fresh
load would otherwise sit there silently.

Only one yt-dlp-backed job runs at a time; a second is refused rather than queued, because two
would fight over the same cache files.
