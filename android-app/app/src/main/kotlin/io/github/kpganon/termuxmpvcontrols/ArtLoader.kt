package io.github.kpganon.termuxmpvcontrols

import android.util.Base64
import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

/**
 * Fetches cover art and hands back raw bytes for [androidx.media3.common.MediaMetadata.artworkData].
 *
 * Media3 prefers artworkData over artworkUri, so loading it here avoids having to teach a custom
 * BitmapLoader about the YouTube fallback: maxresdefault.jpg is missing for plenty of videos, and
 * hqdefault.jpg always exists.
 */
class ArtLoader {

    private val cache = LinkedHashMap<String, ByteArray?>(0, 0.75f, true)

    suspend fun load(art: Protocol.Art?): ByteArray? = when (art) {
        null -> null
        is Protocol.Art.Inline -> decode(art)
        is Protocol.Art.Remote -> fetchCached(art)
    }

    private fun decode(art: Protocol.Art.Inline): ByteArray? = try {
        Base64.decode(art.base64, Base64.DEFAULT)
    } catch (exc: IllegalArgumentException) {
        Log.w(TAG, "bad inline art", exc)
        null
    }

    private suspend fun fetchCached(art: Protocol.Art.Remote): ByteArray? {
        synchronized(cache) {
            if (cache.containsKey(art.url)) return cache[art.url]
        }
        val bytes = withContext(Dispatchers.IO) {
            fetch(art.url) ?: art.fallbackUrl?.let { fetch(it) }
        }
        synchronized(cache) {
            cache[art.url] = bytes
            while (cache.size > MAX_ENTRIES) {
                val oldest = cache.keys.firstOrNull() ?: break
                cache.remove(oldest)
            }
        }
        return bytes
    }

    private fun fetch(url: String): ByteArray? {
        var connection: HttpURLConnection? = null
        return try {
            connection = (URL(url).openConnection() as HttpURLConnection).apply {
                connectTimeout = TIMEOUT_MS
                readTimeout = TIMEOUT_MS
                requestMethod = "GET"
            }
            if (connection.responseCode != HttpURLConnection.HTTP_OK) {
                Log.d(TAG, "art $url returned ${connection.responseCode}")
                return null
            }
            val bytes = connection.inputStream.use { it.readBytes() }
            if (bytes.size > MAX_BYTES) {
                Log.d(TAG, "art $url too large (${bytes.size} bytes)")
                null
            } else {
                bytes
            }
        } catch (exc: IOException) {
            Log.d(TAG, "art $url failed: ${exc.message}")
            null
        } finally {
            connection?.disconnect()
        }
    }

    private companion object {
        const val TAG = "MpvArtLoader"
        const val TIMEOUT_MS = 5000
        const val MAX_BYTES = 4 * 1024 * 1024
        const val MAX_ENTRIES = 32
    }
}
