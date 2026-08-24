package io.github.kpganon.termuxmpvcontrols

import android.content.Context
import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.IOException

/**
 * Starred tracks, kept as a JSON file in the app's private storage.
 *
 * Deliberately not Room or kotlinx.serialization: both need a compiler plugin, and this project
 * runs on AGP 9's built-in Kotlin where adding one is a known rabbit hole. A few hundred entries
 * in a flat file is not a performance problem, and `org.json` is already used by [Protocol].
 *
 * App-private storage means a reinstall wipes this, which happens constantly during development
 * -- hence [exportTo] and [importFrom].
 */
class FavoritesStore(context: Context) {

    data class Favorite(
        val url: String,
        val title: String,
        val artist: String? = null,
        val artUrl: String? = null,
        val addedAt: Long = System.currentTimeMillis(),
    )

    private val file = File(context.filesDir, FILE_NAME)
    private val entries = LinkedHashMap<String, Favorite>()
    private var loaded = false

    @Synchronized
    fun all(): List<Favorite> {
        ensureLoaded()
        // Newest first: the thing you just starred should be at the top.
        return entries.values.sortedByDescending { it.addedAt }
    }

    @Synchronized
    fun contains(url: String?): Boolean {
        if (url.isNullOrEmpty()) return false
        ensureLoaded()
        return entries.containsKey(url)
    }

    @Synchronized
    fun add(favorite: Favorite) {
        ensureLoaded()
        entries[favorite.url] = favorite
        persist()
    }

    @Synchronized
    fun remove(url: String) {
        ensureLoaded()
        if (entries.remove(url) != null) persist()
    }

    /** Returns true if the track is a favorite after the toggle. */
    @Synchronized
    fun toggle(favorite: Favorite): Boolean {
        ensureLoaded()
        return if (entries.containsKey(favorite.url)) {
            entries.remove(favorite.url)
            persist()
            false
        } else {
            entries[favorite.url] = favorite
            persist()
            true
        }
    }

    @Synchronized
    fun exportTo(target: File): Int {
        ensureLoaded()
        target.parentFile?.mkdirs()
        target.writeText(serialise(), Charsets.UTF_8)
        return entries.size
    }

    /** Merges the file into the current set; returns how many were added. */
    @Synchronized
    fun importFrom(source: File): Int {
        ensureLoaded()
        val before = entries.size
        parse(source.readText(Charsets.UTF_8)).forEach { entries.putIfAbsent(it.url, it) }
        persist()
        return entries.size - before
    }

    private fun ensureLoaded() {
        if (loaded) return
        loaded = true
        if (!file.exists()) return
        try {
            parse(file.readText(Charsets.UTF_8)).forEach { entries[it.url] = it }
        } catch (exc: Exception) {
            // A corrupt file must not make the app unusable; it rebuilds as tracks are starred.
            Log.w(TAG, "discarding unreadable favorites", exc)
        }
    }

    private fun parse(raw: String): List<Favorite> {
        val array = JSONArray(raw)
        return (0 until array.length()).mapNotNull { i ->
            val item = array.optJSONObject(i) ?: return@mapNotNull null
            val url = item.optString("url").takeIf { it.isNotEmpty() } ?: return@mapNotNull null
            Favorite(
                url = url,
                title = item.optString("title", url),
                artist = item.optString("artist").takeIf { it.isNotEmpty() },
                artUrl = item.optString("artUrl").takeIf { it.isNotEmpty() },
                addedAt = item.optLong("addedAt", System.currentTimeMillis()),
            )
        }
    }

    private fun serialise(): String {
        val array = JSONArray()
        entries.values.forEach { favorite ->
            array.put(
                JSONObject()
                    .put("url", favorite.url)
                    .put("title", favorite.title)
                    .put("artist", favorite.artist)
                    .put("artUrl", favorite.artUrl)
                    .put("addedAt", favorite.addedAt)
            )
        }
        return array.toString()
    }

    private fun persist() {
        try {
            file.writeText(serialise(), Charsets.UTF_8)
        } catch (exc: IOException) {
            Log.w(TAG, "could not save favorites", exc)
        }
    }

    companion object {
        const val TAG = "MpvFavorites"
        const val FILE_NAME = "favorites.json"
        const val EXPORT_NAME = "termux-mpv-favorites.json"
    }
}
