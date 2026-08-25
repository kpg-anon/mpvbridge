package io.github.kpganon.mpvbridge

import android.app.PendingIntent
import android.content.Intent
import android.os.Bundle
import android.os.Looper
import android.util.Log
import android.view.KeyEvent
import androidx.media3.common.C
import androidx.media3.common.util.UnstableApi
import androidx.media3.session.CommandButton
import androidx.media3.session.MediaSession
import androidx.media3.session.MediaSessionService
import androidx.media3.session.SessionCommand
import androidx.media3.session.SessionResult
import com.google.common.util.concurrent.Futures
import com.google.common.util.concurrent.ListenableFuture
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.launch

/**
 * Hosts the [MediaSession] that Android routes media button events to, and keeps it fed from the
 * mpv bridge. Runs as a `mediaPlayback` foreground service so it survives the screen going off.
 *
 * Anything Media3's [androidx.media3.common.Player] interface has no concept of -- shuffling mpv's
 * playlist, re-checking the source for new tracks, playing an arbitrary URL -- travels as a custom
 * session command from the activity.
 */
@UnstableApi
class MpvSessionService : MediaSessionService() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val silentAudio = SilentAudio()
    private val artLoader = ArtLoader()

    private lateinit var player: MpvPlayer
    private lateinit var bridge: BridgeClient
    private lateinit var settings: Settings
    private var session: MediaSession? = null

    /** One resume attempt per connection, so a reconnect mid-listen never reloads over you. */
    private var resumeAttempted = false

    override fun onCreate() {
        super.onCreate()
        settings = Settings(this)

        player = MpvPlayer(Looper.getMainLooper(), ::onPlayerCommand)
        session = MediaSession.Builder(this, player)
            .setId("mpv")
            .setSessionActivity(openAppIntent())
            .setCallback(SessionCallback())
            .build()

        bridge = BridgeClient(scope, settings)
        bridge.onBye = {
            Log.i(TAG, "mpv exited; retiring the session")
            silentAudio.stop()
            stopSelf()
        }
        bridge.onConnected = {
            resumeAttempted = false
            if (settings.refreshOnLaunch) {
                Log.i(TAG, "checking the source playlist for new tracks")
                bridge.send(Protocol.refreshPlaylist())
            }
        }

        scope.launch { observeBridge() }
        scope.launch { observeRefresh() }
        scope.launch { observeLibrary() }
        scope.launch { observeStatus() }
        bridge.start()
    }

    /**
     * Where the notification and the lockscreen card go when tapped.
     *
     * Without a session activity the media card is inert -- Media3 has nothing to put in the
     * notification's content intent, so a tap does nothing at all. The launcher intent is used in
     * preference to an explicit [MainActivity] one because it resumes the existing task the way
     * tapping the icon does, rather than starting a second copy of the activity on top of it.
     */
    private fun openAppIntent(): PendingIntent {
        val intent = packageManager.getLaunchIntentForPackage(packageName)
            ?: Intent(this, MainActivity::class.java)
        intent.addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP)
        return PendingIntent.getActivity(
            this,
            0,
            intent,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
    }

    private suspend fun observeBridge() {
        combine(bridge.state, bridge.playlist, bridge.status) { state, playlist, status ->
            Triple(state, playlist, status)
        }.collectLatest { (state, playlist, status) ->
            val connected = status == BridgeClient.Status.CONNECTED

            // The keep-alive has to run for as long as we are connected, not just while playing:
            // stopping it while paused would hand the media button session back to whatever app
            // last played audio, and the headset could no longer resume us.
            if (connected && settings.keepAliveEnabled) silentAudio.start() else silentAudio.stop()

            if (connected && bridge.hasState.value) {
                state.sourceUrl?.let { settings.lastPlaylistUrl = it }
                resumeLastPlaylistIfIdle(state)
            }

            val tracks = buildTracks(state, playlist)
            val artData = artLoader.load(state.art)

            player.snapshot = MpvPlayer.Snapshot(
                playing = connected && state.playing,
                index = state.index,
                positionMs = state.positionMs,
                durationMs = state.durationMs ?: C.TIME_UNSET,
                artData = artData,
                playlist = tracks,
            )
        }
    }

    /**
     * Play what was playing last time, when the daemon has come up with nothing loaded.
     *
     * That is the normal state for a daemon the app started itself: `mpvbridge` with no media
     * argument starts mpv idle and waits to be told what to play.
     */
    private fun resumeLastPlaylistIfIdle(state: Protocol.State) {
        if (resumeAttempted || !settings.resumeLastPlaylist) return
        resumeAttempted = true
        if (state.count > 0 || state.sourceUrl != null) return
        val url = settings.lastPlaylistUrl ?: return
        Log.i(TAG, "mpv is idle; loading the last playlist")
        bridge.send(Protocol.loadPlaylist(url))
    }

    private suspend fun observeLibrary() {
        bridge.library.collectLatest { broadcastLibrary() }
    }

    private fun broadcastLibrary() {
        broadcast(
            Commands.LIBRARY_STATUS,
            Bundle().apply {
                putString("playlists", Protocol.encodeLibrary(bridge.library.value))
            },
        )
    }

    /**
     * Tell the activity whether the daemon is reachable, so it can offer to start it in Termux.
     *
     * The activity cannot infer this from the [androidx.media3.common.Player]: a daemon that is
     * up but idle looks exactly like no daemon at all -- an empty timeline, nothing playing.
     */
    private suspend fun observeStatus() {
        bridge.status.collectLatest { broadcastStatus() }
    }

    private fun broadcastStatus() {
        broadcast(
            Commands.BRIDGE_STATUS,
            Bundle().apply {
                putBoolean("connected", bridge.status.value == BridgeClient.Status.CONNECTED)
                putString("mpv", bridge.mpvVersion.value)
            },
        )
    }

    private fun broadcast(action: String, args: Bundle) {
        session?.broadcastCustomCommand(SessionCommand(action, Bundle.EMPTY), args)
    }

    private suspend fun observeRefresh() {
        bridge.refresh.collectLatest { refresh ->
            val current = refresh ?: return@collectLatest
            broadcast(
                Commands.REFRESH_STATUS,
                Bundle().apply {
                    putString("status", current.status)
                    putInt("added", current.added)
                    putInt("total", current.total)
                    putString("reason", current.reason)
                    putString("title", current.title)
                    putString("url", current.url)
                    putString("kind", current.kind)
                },
            )
        }
    }

    private fun buildTracks(
        state: Protocol.State,
        playlist: List<Protocol.PlaylistEntry>,
    ): List<MpvPlayer.Track> {
        if (playlist.isEmpty()) {
            if (state.title.isEmpty() && state.count == 0) return emptyList()
            // The playlist message can arrive after the first state message. Synthesise entries
            // keyed by index so the uids stay identical once the real titles land -- otherwise
            // Media3 sees the whole timeline replaced, loses the item it was on, and issues a
            // corrective seek that we would forward to mpv as a real jump.
            val size = maxOf(state.count, state.index + 1, 1)
            return (0 until size).map { i ->
                MpvPlayer.Track(
                    id = i.toString(),
                    title = if (i == state.index) state.title else "Track ${i + 1}",
                    artist = if (i == state.index) state.artist else null,
                    album = if (i == state.index) state.album else null,
                    url = if (i == state.index) state.url else null,
                )
            }
        }
        return playlist.map { entry ->
            val isCurrent = entry.index == state.index
            MpvPlayer.Track(
                id = entry.index.toString(),
                // mpv's media-title for the current entry is better resolved than the playlist
                // entry's own title, so prefer it where we have it.
                title = if (isCurrent && state.title.isNotEmpty()) state.title else entry.title,
                artist = if (isCurrent) state.artist else null,
                album = if (isCurrent) state.album else null,
                url = entry.url,
                unavailable = entry.unavailable,
            )
        }
    }

    private fun onPlayerCommand(command: MpvPlayer.Command) {
        Log.i(TAG, "command from session: $command")
        val line = when (command) {
            MpvPlayer.Command.Play -> Protocol.command("play")
            MpvPlayer.Command.Pause -> Protocol.command("pause")
            MpvPlayer.Command.Next -> Protocol.command("next")
            MpvPlayer.Command.Previous -> Protocol.command("previous")
            MpvPlayer.Command.Stop -> Protocol.command("stop")
            is MpvPlayer.Command.SeekTo -> Protocol.seek(command.positionMs)
            is MpvPlayer.Command.GoTo -> Protocol.goto(command.index)
        }
        bridge.send(line)
    }

    override fun onGetSession(controllerInfo: MediaSession.ControllerInfo): MediaSession? = session

    override fun onTaskRemoved(rootIntent: Intent?) {
        // Keep going after the task is swiped away; mpv is still running in Termux.
    }

    override fun onDestroy() {
        silentAudio.stop()
        bridge.stop()
        scope.cancel()
        session?.run {
            player.release()
            release()
        }
        session = null
        super.onDestroy()
    }

    private inner class SessionCallback : MediaSession.Callback {

        override fun onConnect(
            session: MediaSession,
            controller: MediaSession.ControllerInfo,
        ): MediaSession.ConnectionResult {
            val commands = MediaSession.ConnectionResult.DEFAULT_SESSION_COMMANDS.buildUpon()
                .add(SessionCommand(Commands.SHUFFLE, Bundle.EMPTY))
                .add(SessionCommand(Commands.UNSHUFFLE, Bundle.EMPTY))
                .add(SessionCommand(Commands.REFRESH_PLAYLIST, Bundle.EMPTY))
                .add(SessionCommand(Commands.PLAY_URL, Bundle.EMPTY))
                .add(SessionCommand(Commands.REFRESH_STATUS, Bundle.EMPTY))
                .add(SessionCommand(Commands.ADD_PLAYLIST, Bundle.EMPTY))
                .add(SessionCommand(Commands.LOAD_PLAYLIST, Bundle.EMPTY))
                .add(SessionCommand(Commands.REMOVE_PLAYLIST, Bundle.EMPTY))
                .add(SessionCommand(Commands.REQUEST_LIBRARY, Bundle.EMPTY))
                .add(SessionCommand(Commands.LIBRARY_STATUS, Bundle.EMPTY))
                .add(SessionCommand(Commands.BRIDGE_STATUS, Bundle.EMPTY))
                .build()
            return MediaSession.ConnectionResult.AcceptedResultBuilder(session)
                .setAvailableSessionCommands(commands)
                .build()
        }

        override fun onCustomCommand(
            session: MediaSession,
            controller: MediaSession.ControllerInfo,
            customCommand: SessionCommand,
            args: Bundle,
        ): ListenableFuture<SessionResult> {
            Log.i(TAG, "custom command: ${customCommand.customAction}")
            when (customCommand.customAction) {
                Commands.SHUFFLE -> bridge.send(Protocol.command("shuffle"))
                Commands.UNSHUFFLE -> bridge.send(Protocol.command("unshuffle"))
                Commands.REFRESH_PLAYLIST -> bridge.send(Protocol.refreshPlaylist())
                Commands.PLAY_URL -> args.getString("url")
                    ?.takeIf { it.isNotBlank() }
                    ?.let { bridge.send(Protocol.playUrl(it)) }

                Commands.REQUEST_LIBRARY -> {
                    // The activity connects to the session long after the service started, so it
                    // has missed every broadcast up to now. Answer from what we already hold
                    // before asking the daemon for anything.
                    broadcastStatus()
                    broadcastLibrary()
                    bridge.send(Protocol.library())
                }
                Commands.ADD_PLAYLIST -> withUrl(args) { bridge.send(Protocol.addPlaylist(it)) }
                Commands.REMOVE_PLAYLIST -> withUrl(args) {
                    if (it == settings.lastPlaylistUrl) settings.lastPlaylistUrl = null
                    bridge.send(Protocol.removePlaylist(it))
                }

                Commands.LOAD_PLAYLIST -> withUrl(args) {
                    // Remember it now rather than waiting for the daemon to echo a source back,
                    // so a load that is still resolving still survives the app being reopened.
                    settings.lastPlaylistUrl = it
                    resumeAttempted = true
                    bridge.send(Protocol.loadPlaylist(it))
                }

                else -> return Futures.immediateFuture(
                    SessionResult(SessionResult.RESULT_ERROR_NOT_SUPPORTED)
                )
            }
            return Futures.immediateFuture(SessionResult(SessionResult.RESULT_SUCCESS))
        }

        override fun onMediaButtonEvent(
            session: MediaSession,
            controllerInfo: MediaSession.ControllerInfo,
            intent: Intent,
        ): Boolean {
            val key = keyEventOf(intent)
            Log.i(
                TAG,
                "onMediaButtonEvent from=${controllerInfo.packageName} " +
                    "action=${key?.action} keyCode=${key?.keyCode} " +
                    "name=${key?.let { KeyEvent.keyCodeToString(it.keyCode) }}",
            )
            // false = let Media3 apply its default mapping onto the Player.
            return false
        }

        private inline fun withUrl(args: Bundle, action: (String) -> Unit) {
            args.getString("url")?.takeIf { it.isNotBlank() }?.let(action)
        }

        @Suppress("DEPRECATION")
        private fun keyEventOf(intent: Intent): KeyEvent? =
            if (android.os.Build.VERSION.SDK_INT >= 33) {
                intent.getParcelableExtra(Intent.EXTRA_KEY_EVENT, KeyEvent::class.java)
            } else {
                intent.getParcelableExtra(Intent.EXTRA_KEY_EVENT)
            }
    }

    /** Custom session commands, for the things Media3's Player interface has no concept of. */
    object Commands {
        private const val PREFIX = "io.github.kpganon.mpvbridge."

        const val SHUFFLE = PREFIX + "SHUFFLE"
        const val UNSHUFFLE = PREFIX + "UNSHUFFLE"
        const val REFRESH_PLAYLIST = PREFIX + "REFRESH_PLAYLIST"
        const val PLAY_URL = PREFIX + "PLAY_URL"
        const val ADD_PLAYLIST = PREFIX + "ADD_PLAYLIST"
        const val LOAD_PLAYLIST = PREFIX + "LOAD_PLAYLIST"
        const val REMOVE_PLAYLIST = PREFIX + "REMOVE_PLAYLIST"
        const val REQUEST_LIBRARY = PREFIX + "REQUEST_LIBRARY"

        /** Service to controller: progress of a running playlist fetch. */
        const val REFRESH_STATUS = PREFIX + "REFRESH_STATUS"

        /** Service to controller: the saved playlists the daemon knows about. */
        const val LIBRARY_STATUS = PREFIX + "LIBRARY_STATUS"

        /** Service to controller: whether the daemon is reachable at all. */
        const val BRIDGE_STATUS = PREFIX + "BRIDGE_STATUS"
    }

    companion object {
        const val TAG = "MpvSession"
    }
}
