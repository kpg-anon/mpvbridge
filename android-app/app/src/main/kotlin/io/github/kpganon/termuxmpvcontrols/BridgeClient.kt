package io.github.kpganon.termuxmpvcontrols

import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import java.io.BufferedWriter
import java.io.IOException
import java.net.InetSocketAddress
import java.net.Socket

/**
 * Keeps a line-JSON connection to `mpvbridge` on loopback, reconnecting whenever mpv is not
 * running. State flows out through [state] and [playlist]; commands go in through [send].
 *
 * There is deliberately no outbound queue. An earlier version ran one writer coroutine per
 * connection, all draining a single shared channel, so after a reconnect a command could be
 * written into the previous, dead socket and vanish. Commands are rare, so each [send] just takes
 * the write lock and writes to whichever socket is current -- or is dropped loudly if there is
 * none.
 */
class BridgeClient(
    private val scope: CoroutineScope,
    private val settings: Settings,
) {

    enum class Status { DISCONNECTED, CONNECTING, CONNECTED }

    private val _state = MutableStateFlow(Protocol.State())
    val state: StateFlow<Protocol.State> = _state.asStateFlow()

    private val _playlist = MutableStateFlow<List<Protocol.PlaylistEntry>>(emptyList())
    val playlist: StateFlow<List<Protocol.PlaylistEntry>> = _playlist.asStateFlow()

    private val _status = MutableStateFlow(Status.DISCONNECTED)
    val status: StateFlow<Status> = _status.asStateFlow()

    private val _mpvVersion = MutableStateFlow<String?>(null)
    val mpvVersion: StateFlow<String?> = _mpvVersion.asStateFlow()

    private val _refresh = MutableStateFlow<Protocol.Refresh?>(null)
    val refresh: StateFlow<Protocol.Refresh?> = _refresh.asStateFlow()

    /** Fires once each time the socket comes up, for connect-time actions. */
    var onConnected: (() -> Unit)? = null

    /** Fires when the daemon says mpv exited, so the service can retire the notification. */
    var onBye: (() -> Unit)? = null

    private val writeLock = Mutex()
    private var writer: BufferedWriter? = null
    private var socket: Socket? = null
    private var job: Job? = null

    fun start() {
        if (job?.isActive == true) {
            Log.d(TAG, "already running")
            return
        }
        job = scope.launch(Dispatchers.IO) { connectionLoop() }
    }

    fun stop() {
        job?.cancel()
        job = null
        closeCurrent("stop() called")
        _status.value = Status.DISCONNECTED
    }

    fun send(line: String) {
        scope.launch(Dispatchers.IO) {
            writeLock.withLock {
                val target = writer
                if (target == null) {
                    Log.w(TAG, "not connected; dropping $line")
                    return@withLock
                }
                try {
                    target.write(line)
                    target.write("\n")
                    target.flush()
                    Log.i(TAG, "sent $line")
                } catch (exc: IOException) {
                    Log.w(TAG, "send failed for $line", exc)
                    closeCurrent("write failed")
                }
            }
        }
    }

    private suspend fun connectionLoop() {
        var backoffMs = MIN_BACKOFF_MS
        while (scope.isActive) {
            _status.value = Status.CONNECTING
            val reason = try {
                runConnection()
            } catch (exc: IOException) {
                "unreachable: ${exc.message}"
            } finally {
                closeCurrent("connection loop iteration ended")
                _status.value = Status.DISCONNECTED
            }
            Log.d(TAG, "connection ended: $reason")
            backoffMs = if (reason.startsWith("unreachable")) {
                (backoffMs * 2).coerceAtMost(MAX_BACKOFF_MS)
            } else {
                MIN_BACKOFF_MS
            }
            delay(backoffMs)
        }
    }

    /** Returns a human-readable reason the connection ended. */
    private suspend fun runConnection(): String {
        val host = settings.host
        val port = settings.port

        val opened = Socket()
        opened.connect(InetSocketAddress(host, port), CONNECT_TIMEOUT_MS)
        opened.soTimeout = 0
        opened.tcpNoDelay = true
        opened.keepAlive = true

        val out = opened.getOutputStream().bufferedWriter()
        writeLock.withLock {
            socket = opened
            writer = out
        }
        Log.i(TAG, "connected to $host:$port")
        _status.value = Status.CONNECTED
        onConnected?.invoke()

        // Always greet: harmless without a token, required with one.
        send(Protocol.hello(settings.token))

        val reader = opened.getInputStream().bufferedReader()
        while (scope.isActive) {
            val line = withContext(Dispatchers.IO) { reader.readLine() }
                ?: return "daemon closed the connection"

            if (line.isBlank()) continue
            val message = try {
                Protocol.parse(line)
            } catch (exc: Exception) {
                Log.w(TAG, "unparseable line: ${line.take(200)}", exc)
                continue
            }

            when (message) {
                is Protocol.Message.Hello -> {
                    Log.i(TAG, "handshake ok, mpv ${message.mpv}")
                    _mpvVersion.value = message.mpv
                }

                is Protocol.Message.StateChanged -> _state.value = message.state
                is Protocol.Message.PlaylistChanged -> {
                    Log.d(TAG, "playlist: ${message.entries.size} entries")
                    _playlist.value = message.entries
                }

                is Protocol.Message.RefreshProgress -> {
                    Log.i(TAG, "refresh: ${message.refresh}")
                    _refresh.value = message.refresh
                }

                is Protocol.Message.Error -> Log.w(TAG, "daemon error: ${message.reason}")
                is Protocol.Message.Unknown -> Log.d(TAG, "ignoring ${message.type}")
                Protocol.Message.Bye -> {
                    _state.value = Protocol.State()
                    _playlist.value = emptyList()
                    onBye?.invoke()
                    return "mpv exited (bye)"
                }
            }
        }
        return "scope cancelled"
    }

    private fun closeCurrent(why: String) {
        val open = socket ?: return
        Log.d(TAG, "closing socket: $why")
        writer = null
        socket = null
        try {
            open.close()
        } catch (exc: IOException) {
            Log.d(TAG, "socket close failed", exc)
        }
    }

    private companion object {
        const val TAG = "MpvBridgeClient"
        const val CONNECT_TIMEOUT_MS = 1500
        const val MIN_BACKOFF_MS = 1000L
        const val MAX_BACKOFF_MS = 15000L
    }
}
