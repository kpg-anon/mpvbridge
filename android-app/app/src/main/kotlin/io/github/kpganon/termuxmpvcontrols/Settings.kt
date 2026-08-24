package io.github.kpganon.termuxmpvcontrols

import android.content.Context

/** Where the daemon is, and how the app should behave. Loopback defaults suit normal Termux use. */
class Settings(context: Context) {

    private val prefs = context.getSharedPreferences("termux-mpv-controls", Context.MODE_PRIVATE)

    var host: String
        get() = prefs.getString(KEY_HOST, DEFAULT_HOST) ?: DEFAULT_HOST
        set(value) = prefs.edit().putString(KEY_HOST, value).apply()

    var port: Int
        get() = prefs.getInt(KEY_PORT, DEFAULT_PORT)
        set(value) = prefs.edit().putInt(KEY_PORT, value).apply()

    var token: String?
        get() = prefs.getString(KEY_TOKEN, null)?.takeIf { it.isNotEmpty() }
        set(value) = prefs.edit().putString(KEY_TOKEN, value.orEmpty()).apply()

    /** Deleted, private and region-blocked entries show as bare URLs; hide them by default. */
    var hideUnavailable: Boolean
        get() = prefs.getBoolean(KEY_HIDE_UNAVAILABLE, true)
        set(value) = prefs.edit().putBoolean(KEY_HIDE_UNAVAILABLE, value).apply()

    /** Ask the daemon to re-check the source playlist for new tracks once connected. */
    var refreshOnLaunch: Boolean
        get() = prefs.getBoolean(KEY_REFRESH_ON_LAUNCH, false)
        set(value) = prefs.edit().putBoolean(KEY_REFRESH_ON_LAUNCH, value).apply()

    /**
     * The silent keep-alive that makes Android route headset buttons here. Turning it off stops
     * bud taps reaching mpv entirely -- see [SilentAudio].
     */
    var keepAliveEnabled: Boolean
        get() = prefs.getBoolean(KEY_KEEP_ALIVE, true)
        set(value) = prefs.edit().putBoolean(KEY_KEEP_ALIVE, value).apply()

    /** The mpv arguments the Start button hands to Termux. */
    var launchCommand: String
        get() = prefs.getString(KEY_LAUNCH_COMMAND, DEFAULT_LAUNCH_COMMAND)
            ?: DEFAULT_LAUNCH_COMMAND
        set(value) = prefs.edit().putString(KEY_LAUNCH_COMMAND, value).apply()

    companion object {
        const val DEFAULT_HOST = "127.0.0.1"
        const val DEFAULT_PORT = 7355
        const val DEFAULT_LAUNCH_COMMAND = "mpvbridge --vo=null"

        private const val KEY_HOST = "host"
        private const val KEY_PORT = "port"
        private const val KEY_TOKEN = "token"
        private const val KEY_HIDE_UNAVAILABLE = "hide_unavailable"
        private const val KEY_REFRESH_ON_LAUNCH = "refresh_on_launch"
        private const val KEY_KEEP_ALIVE = "keep_alive"
        private const val KEY_LAUNCH_COMMAND = "launch_command"
    }
}
