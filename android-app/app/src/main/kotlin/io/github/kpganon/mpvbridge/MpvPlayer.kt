package io.github.kpganon.mpvbridge

import android.os.Bundle
import android.os.Looper
import android.util.Log
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.MediaMetadata
import androidx.media3.common.Player
import androidx.media3.common.SimpleBasePlayer
import androidx.media3.common.util.UnstableApi
import com.google.common.util.concurrent.Futures
import com.google.common.util.concurrent.ListenableFuture

/**
 * A [Player] that owns no audio of its own. Every playback command it receives -- from the
 * notification, the lockscreen, or a Bluetooth media key -- is forwarded to [commandSink], which
 * writes it to the mpv bridge socket. Playback state flows the other way, via [snapshot].
 */
@UnstableApi
class MpvPlayer(
    looper: Looper,
    private val commandSink: (Command) -> Unit,
) : SimpleBasePlayer(looper) {

    data class Track(
        val id: String,
        val title: String,
        val artist: String? = null,
        val album: String? = null,
        val url: String? = null,
        /** A deleted, private or blocked video. Carried through so the UI can hide it. */
        val unavailable: Boolean = false,
    )

    data class Snapshot(
        val playing: Boolean = false,
        val index: Int = 0,
        val positionMs: Long = 0L,
        val durationMs: Long = C.TIME_UNSET,
        val artData: ByteArray? = null,
        val playlist: List<Track> = emptyList(),
    ) {
        // ByteArray needs identity-free equality or every state push looks like a change.
        override fun equals(other: Any?): Boolean {
            if (this === other) return true
            if (other !is Snapshot) return false
            return playing == other.playing &&
                index == other.index &&
                positionMs == other.positionMs &&
                durationMs == other.durationMs &&
                playlist == other.playlist &&
                artData.contentEquals(other.artData)
        }

        override fun hashCode(): Int {
            var result = playing.hashCode()
            result = 31 * result + index
            result = 31 * result + positionMs.hashCode()
            result = 31 * result + durationMs.hashCode()
            result = 31 * result + playlist.hashCode()
            result = 31 * result + (artData?.contentHashCode() ?: 0)
            return result
        }
    }

    sealed interface Command {
        data object Play : Command
        data object Pause : Command
        data object Next : Command
        data object Previous : Command
        data object Stop : Command
        data class SeekTo(val positionMs: Long) : Command
        data class GoTo(val index: Int) : Command
    }

    @Volatile
    var snapshot: Snapshot = Snapshot()
        set(value) {
            if (field == value) return
            field = value
            invalidateState()
        }

    override fun getState(): State {
        val current = snapshot
        val tracks = current.playlist
        if (tracks.isEmpty()) return EMPTY_STATE
        val index = current.index.coerceIn(0, tracks.lastIndex)

        val items = tracks.mapIndexed { i, track ->
            val metadata = track.toMetadata(if (i == index) current.artData else null)
            MediaItemData.Builder(track.id)
                .setMediaItem(
                    MediaItem.Builder().setMediaId(track.id).setMediaMetadata(metadata).build()
                )
                .setMediaMetadata(metadata)
                .setDurationUs(
                    if (i == index && current.durationMs != C.TIME_UNSET) {
                        current.durationMs * 1000L
                    } else {
                        C.TIME_UNSET
                    }
                )
                .setIsSeekable(true)
                .setIsDynamic(false)
                .build()
        }

        return State.Builder()
            .setAvailableCommands(AVAILABLE_COMMANDS)
            .setPlaybackState(Player.STATE_READY)
            .setPlayWhenReady(current.playing, Player.PLAY_WHEN_READY_CHANGE_REASON_REMOTE)
            .setPlaylist(items)
            .setCurrentMediaItemIndex(index)
            // Extrapolating means the seek bar keeps moving between the sparse anchors the daemon
            // sends, instead of needing a position message every second.
            .setContentPositionMs(
                if (current.playing) {
                    PositionSupplier.getExtrapolating(current.positionMs, 1.0f)
                } else {
                    PositionSupplier.getConstant(current.positionMs)
                }
            )
            .build()
    }

    override fun handleSetPlayWhenReady(playWhenReady: Boolean): ListenableFuture<*> {
        Log.i(TAG, "handleSetPlayWhenReady($playWhenReady)")
        commandSink(if (playWhenReady) Command.Play else Command.Pause)
        return Futures.immediateVoidFuture()
    }

    override fun handleStop(): ListenableFuture<*> {
        Log.i(TAG, "handleStop()")
        commandSink(Command.Stop)
        return Futures.immediateVoidFuture()
    }

    override fun handleSeek(
        mediaItemIndex: Int,
        positionMs: Long,
        @Player.Command seekCommand: Int,
    ): ListenableFuture<*> {
        Log.i(TAG, "handleSeek(index=$mediaItemIndex, pos=$positionMs, cmd=${nameOf(seekCommand)})")
        when (seekCommand) {
            Player.COMMAND_SEEK_TO_NEXT,
            Player.COMMAND_SEEK_TO_NEXT_MEDIA_ITEM,
            -> commandSink(Command.Next)

            Player.COMMAND_SEEK_TO_PREVIOUS,
            Player.COMMAND_SEEK_TO_PREVIOUS_MEDIA_ITEM,
            -> commandSink(Command.Previous)

            Player.COMMAND_SEEK_TO_MEDIA_ITEM -> {
                if (mediaItemIndex == snapshot.index) {
                    Log.i(TAG, "ignoring reconciling seek to the current item")
                } else {
                    commandSink(Command.GoTo(mediaItemIndex))
                }
            }

            else -> commandSink(Command.SeekTo(positionMs))
        }
        return Futures.immediateVoidFuture()
    }

    private fun Track.toMetadata(artData: ByteArray?) = MediaMetadata.Builder()
        .setTitle(title)
        .setArtist(artist)
        .setAlbumTitle(album)
        .setIsBrowsable(false)
        .setIsPlayable(true)
        // Media3 has no field for either of these, and the activity only ever sees the timeline
        // through the MediaController, so they travel as extras.
        .setExtras(
            Bundle().apply {
                putString(EXTRA_URL, url)
                putBoolean(EXTRA_UNAVAILABLE, unavailable)
            }
        )
        .apply {
            if (artData != null) {
                setArtworkData(artData, MediaMetadata.PICTURE_TYPE_FRONT_COVER)
            }
        }
        .build()

    private fun nameOf(@Player.Command command: Int) = when (command) {
        Player.COMMAND_SEEK_TO_NEXT -> "SEEK_TO_NEXT"
        Player.COMMAND_SEEK_TO_NEXT_MEDIA_ITEM -> "SEEK_TO_NEXT_MEDIA_ITEM"
        Player.COMMAND_SEEK_TO_PREVIOUS -> "SEEK_TO_PREVIOUS"
        Player.COMMAND_SEEK_TO_PREVIOUS_MEDIA_ITEM -> "SEEK_TO_PREVIOUS_MEDIA_ITEM"
        Player.COMMAND_SEEK_TO_MEDIA_ITEM -> "SEEK_TO_MEDIA_ITEM"
        Player.COMMAND_SEEK_IN_CURRENT_MEDIA_ITEM -> "SEEK_IN_CURRENT_MEDIA_ITEM"
        else -> "command:$command"
    }

    companion object {
        const val TAG = "MpvPlayer"

        const val EXTRA_URL = "mpv.url"
        const val EXTRA_UNAVAILABLE = "mpv.unavailable"

        private val EMPTY_STATE = State.Builder()
            .setAvailableCommands(Player.Commands.EMPTY)
            .setPlaybackState(Player.STATE_IDLE)
            .setPlayWhenReady(false, Player.PLAY_WHEN_READY_CHANGE_REASON_REMOTE)
            .build()

        private val AVAILABLE_COMMANDS = Player.Commands.Builder()
            .addAll(
                Player.COMMAND_PLAY_PAUSE,
                Player.COMMAND_STOP,
                Player.COMMAND_SEEK_TO_NEXT,
                Player.COMMAND_SEEK_TO_NEXT_MEDIA_ITEM,
                Player.COMMAND_SEEK_TO_PREVIOUS,
                Player.COMMAND_SEEK_TO_PREVIOUS_MEDIA_ITEM,
                Player.COMMAND_SEEK_IN_CURRENT_MEDIA_ITEM,
                Player.COMMAND_SEEK_TO_MEDIA_ITEM,
                Player.COMMAND_GET_TIMELINE,
                Player.COMMAND_GET_CURRENT_MEDIA_ITEM,
                Player.COMMAND_GET_METADATA,
            )
            .build()
    }
}
