package io.github.kpganon.termuxmpvcontrols

import org.json.JSONObject

/** Client half of the wire format documented in `docs/protocol.md`. */
object Protocol {

    const val VERSION = 1

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
    )

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
    ) {
        val isRunning: Boolean get() = status == "running"
    }

    sealed interface Message {
        data class Hello(val version: Int, val mpv: String?) : Message
        data class StateChanged(val state: State) : Message
        data class PlaylistChanged(val entries: List<PlaylistEntry>) : Message
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
            "bye" -> Message.Bye
            "refresh" -> Message.RefreshProgress(
                Refresh(
                    status = json.optString("status", "error"),
                    added = json.optInt("added", 0),
                    total = json.optInt("total", 0),
                    reason = json.optStringOrNull("reason"),
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
