package io.github.kpganon.mpvbridge

import org.json.JSONArray
import org.json.JSONObject

/** Client half of the wire format documented in `docs/protocol.md`. */
object Protocol {

    const val VERSION = 1

    const val KIND_RECHECK = "refresh"
    const val KIND_ADD = "add"
    const val KIND_LOAD = "load"

    sealed interface Art {
        /** A remote image, with a second URL to try when the first 404s. */
        data class Remote(val url: String, val fallbackUrl: String?) : Art

        /** Bytes inlined by the daemon, for local files with a sidecar cover. */
        data class Inline(val base64: String, val mime: String) : Art
    }

    data class State(
        val playing: Boolean = false,
        val title: String = "",
        val artist: String? = null,
        val album: String? = null,
        val index: Int = 0,
        val count: Int = 0,
        val positionMs: Long = 0L,
        val durationMs: Long? = null,
        val art: Art? = null,
        val idle: Boolean = true,
        val url: String? = null,
        /** The playlist these entries came from. Null when mpv was handed loose files. */
        val sourceUrl: String? = null,
        val sourceTitle: String? = null,
    )

    /** One playlist the daemon has cached and can start playing without re-resolving it. */
    data class SavedPlaylist(
        val url: String,
        val title: String? = null,
        val count: Int = 0,
        val fetched: Long = 0L,
        val current: Boolean = false,
    ) {
        /** An unnamed playlist is one yt-dlp gave no title for; the URL is all we have. */
        val displayTitle: String get() = title?.takeIf { it.isNotBlank() } ?: url
    }

    data class PlaylistEntry(
        val index: Int,
        val title: String,
        val current: Boolean,
        val url: String? = null,
        /** A deleted, private or region-blocked video: mpv never resolved a title for it. */
        val unavailable: Boolean = false,
    )

    data class Refresh(
        val status: String,
        val added: Int = 0,
        val total: Int = 0,
        val reason: String? = null,
        /** Which playlist this is about -- adding one and re-checking one look the same here. */
        val title: String? = null,
        val url: String? = null,
        /** Which job it is: `refresh`, `add` or `load`. They word differently. */
        val kind: String = KIND_RECHECK,
    ) {
        val isRunning: Boolean get() = status == "running"
    }

    sealed interface Message {
        data class Hello(val version: Int, val mpv: String?) : Message
        data class StateChanged(val state: State) : Message
        data class PlaylistChanged(val entries: List<PlaylistEntry>) : Message
        data class LibraryChanged(val playlists: List<SavedPlaylist>) : Message
        data object Bye : Message
        data class Error(val reason: String) : Message
        data class RefreshProgress(val refresh: Refresh) : Message
        data class Unknown(val type: String?) : Message
    }

    fun parse(line: String): Message {
        val json = JSONObject(line)
        return when (val type = json.optString("type")) {
            "hello" -> Message.Hello(
                version = json.optInt("version", VERSION),
                mpv = json.optStringOrNull("mpv"),
            )

            "state" -> Message.StateChanged(parseState(json))
            "playlist" -> Message.PlaylistChanged(parsePlaylist(json))
            "library" -> Message.LibraryChanged(parseLibrary(json))
            "bye" -> Message.Bye
            "refresh" -> Message.RefreshProgress(
                Refresh(
                    status = json.optString("status", "error"),
                    added = json.optInt("added", 0),
                    total = json.optInt("total", 0),
                    reason = json.optStringOrNull("reason"),
                    title = json.optStringOrNull("title"),
                    url = json.optStringOrNull("url"),
                    kind = json.optString("kind", KIND_RECHECK),
                )
            )
            "error" -> Message.Error(json.optString("reason", "unknown"))
            else -> Message.Unknown(type)
        }
    }

    private fun parseState(json: JSONObject): State = State(
        playing = json.optBoolean("playing", false),
        title = json.optString("title", ""),
        artist = json.optStringOrNull("artist"),
        album = json.optStringOrNull("album"),
        index = json.optInt("index", 0),
        count = json.optInt("count", 0),
        positionMs = (json.optDouble("position", 0.0) * 1000).toLong(),
        durationMs = json.optDoubleOrNull("duration")?.let { (it * 1000).toLong() },
        art = json.optJSONObject("art")?.let(::parseArt),
        idle = json.optBoolean("idle", true),
        url = json.optStringOrNull("url"),
        sourceUrl = json.optJSONObject("source")?.optStringOrNull("url"),
        sourceTitle = json.optJSONObject("source")?.optStringOrNull("title"),
    )

    private fun parseArt(json: JSONObject): Art? {
        json.optStringOrNull("data")?.let { data ->
            return Art.Inline(base64 = data, mime = json.optString("mime", "image/jpeg"))
        }
        json.optStringOrNull("url")?.let { url ->
            return Art.Remote(url = url, fallbackUrl = json.optStringOrNull("fallbackUrl"))
        }
        return null
    }

    private fun parsePlaylist(json: JSONObject): List<PlaylistEntry> {
        val array = json.optJSONArray("entries") ?: return emptyList()
        return (0 until array.length()).mapNotNull { i ->
            val entry = array.optJSONObject(i) ?: return@mapNotNull null
            PlaylistEntry(
                index = entry.optInt("index", i),
                title = entry.optString("title", ""),
                current = entry.optBoolean("current", false),
                url = entry.optStringOrNull("url"),
                unavailable = entry.optBoolean("unavailable", false),
            )
        }
    }

    private fun parseLibrary(json: JSONObject): List<SavedPlaylist> {
        val array = json.optJSONArray("playlists") ?: return emptyList()
        return (0 until array.length()).mapNotNull { i ->
            array.optJSONObject(i)?.let(::parseSavedPlaylist)
        }
    }

    private fun parseSavedPlaylist(json: JSONObject): SavedPlaylist? {
        val url = json.optStringOrNull("url") ?: return null
        return SavedPlaylist(
            url = url,
            title = json.optStringOrNull("title"),
            count = json.optInt("count", 0),
            fetched = json.optLong("fetched", 0L),
            current = json.optBoolean("current", false),
        )
    }

    /**
     * The library re-encoded for the trip from the service to the activity.
     *
     * That hop is a session custom command, whose payload is a [android.os.Bundle]; a JSON string
     * keeps the shape identical to the wire format rather than inventing a second one.
     */
    fun encodeLibrary(playlists: List<SavedPlaylist>): String {
        val array = JSONArray()
        for (playlist in playlists) {
            array.put(
                JSONObject()
                    .put("url", playlist.url)
                    .put("title", playlist.title ?: JSONObject.NULL)
                    .put("count", playlist.count)
                    .put("fetched", playlist.fetched)
                    .put("current", playlist.current)
            )
        }
        return array.toString()
    }

    fun decodeLibrary(encoded: String?): List<SavedPlaylist> {
        if (encoded.isNullOrEmpty()) return emptyList()
        val array = runCatching { JSONArray(encoded) }.getOrNull() ?: return emptyList()
        return (0 until array.length()).mapNotNull { i ->
            array.optJSONObject(i)?.let(::parseSavedPlaylist)
        }
    }

    fun hello(token: String?): String =
        JSONObject().put("type", "hello").put("version", VERSION).apply {
            if (!token.isNullOrEmpty()) put("token", token)
        }.toString()

    fun command(name: String): String =
        JSONObject().put("type", "cmd").put("name", name).toString()

    fun seek(positionMs: Long): String =
        JSONObject().put("type", "cmd").put("name", "seek")
            .put("position", positionMs / 1000.0).toString()

    fun goto(index: Int): String =
        JSONObject().put("type", "cmd").put("name", "goto").put("index", index).toString()

    fun playUrl(url: String): String =
        JSONObject().put("type", "cmd").put("name", "play-url").put("url", url).toString()

    fun refreshPlaylist(): String =
        JSONObject().put("type", "cmd").put("name", "refresh-playlist").toString()

    fun library(): String = command("library")

    fun addPlaylist(url: String): String = playlistCommand("add-playlist", url)

    fun loadPlaylist(url: String): String = playlistCommand("load-playlist", url)

    fun removePlaylist(url: String): String = playlistCommand("remove-playlist", url)

    private fun playlistCommand(name: String, url: String): String =
        JSONObject().put("type", "cmd").put("name", name).put("url", url).toString()

    private fun JSONObject.optStringOrNull(key: String): String? {
        if (!has(key) || isNull(key)) return null
        return optString(key).takeIf { it.isNotEmpty() }
    }

    private fun JSONObject.optDoubleOrNull(key: String): Double? {
        if (!has(key) || isNull(key)) return null
        val value = optDouble(key)
        return if (value.isNaN()) null else value
    }
}
