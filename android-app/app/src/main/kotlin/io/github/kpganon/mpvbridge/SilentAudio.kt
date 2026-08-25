package io.github.kpganon.mpvbridge

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import android.util.Log

/**
 * A looping, inaudible PCM track that exists only to make Android route media buttons here.
 *
 * `MediaSessionStack.updateMediaButtonSessionIfNeeded()` picks the media button session by walking
 * the UIDs that are currently producing audio and taking the first one that owns a session. mpv's
 * audio belongs to Termux's UID, which owns no session, and this app owns a session but plays
 * nothing -- so without this the media button session stays null and a Buds tap goes nowhere.
 * Verified on a Galaxy S24+ (One UI 8.0.5, Android 16) with `dumpsys media_session`.
 *
 * The track is one second of zeroes looped forever in [AudioTrack.MODE_STATIC], so it costs no
 * CPU beyond keeping the audio path open, and it never requests audio focus, so it cannot duck or
 * interrupt mpv.
 */
class SilentAudio {

    private var track: AudioTrack? = null

    val isRunning: Boolean
        get() = track != null

    fun start() {
        if (track != null) return
        try {
            val frames = SAMPLE_RATE
            val bytes = frames * BYTES_PER_FRAME
            val built = AudioTrack.Builder()
                .setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_MEDIA)
                        .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                        .build()
                )
                .setAudioFormat(
                    AudioFormat.Builder()
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                        .setSampleRate(SAMPLE_RATE)
                        .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                        .build()
                )
                .setBufferSizeInBytes(bytes)
                .setTransferMode(AudioTrack.MODE_STATIC)
                .build()

            built.write(ByteArray(bytes), 0, bytes)
            built.setLoopPoints(0, frames, LOOP_FOREVER)
            built.setVolume(0f)
            built.play()
            track = built
            Log.i(TAG, "silent keep-alive started")
        } catch (exc: Exception) {
            Log.w(TAG, "could not start silent keep-alive", exc)
            release()
        }
    }

    fun stop() {
        val current = track ?: return
        try {
            current.pause()
            current.flush()
        } catch (exc: IllegalStateException) {
            Log.d(TAG, "silent keep-alive already stopped", exc)
        }
        release()
        Log.i(TAG, "silent keep-alive stopped")
    }

    private fun release() {
        try {
            track?.release()
        } catch (exc: Exception) {
            Log.d(TAG, "release failed", exc)
        }
        track = null
    }

    private companion object {
        const val TAG = "MpvSilentAudio"
        const val SAMPLE_RATE = 44100
        const val BYTES_PER_FRAME = 2 // 16-bit mono
        const val LOOP_FOREVER = -1
    }
}
