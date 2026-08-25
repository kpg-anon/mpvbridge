package io.github.kpganon.mpvbridge

import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.util.Log
import androidx.core.content.ContextCompat

/**
 * Starts the bridge inside Termux, so playback does not have to be launched by hand.
 *
 * This uses Termux's `RunCommandService`, which is guarded by `com.termux.permission.RUN_COMMAND`.
 * An app can hold that permission because it declares it in its own manifest -- which is exactly
 * why this works here and not over adb, where the caller is `com.android.shell` and never declares
 * it, making `pm grant` a silent no-op.
 *
 * Termux also requires `allow-external-apps=true` in `~/.termux/termux.properties`. There is no way
 * to read that setting from here, so a failure to start is reported as a likely cause rather than
 * a certainty.
 */
object TermuxLauncher {

    const val PERMISSION = "com.termux.permission.RUN_COMMAND"

    private const val TAG = "MpvTermuxLauncher"
    private const val PACKAGE = "com.termux"
    private const val SERVICE = "com.termux.app.RunCommandService"
    private const val ACTION = "com.termux.RUN_COMMAND"
    private const val BASH = "/data/data/com.termux/files/usr/bin/bash"
    private const val HOME = "/data/data/com.termux/files/home"

    sealed interface Result {
        data object Started : Result
        data object TermuxMissing : Result
        data object PermissionMissing : Result
        data class Failed(val reason: String) : Result
    }

    fun isInstalled(context: Context): Boolean = try {
        context.packageManager.getPackageInfo(PACKAGE, 0)
        true
    } catch (_: PackageManager.NameNotFoundException) {
        false
    }

    fun hasPermission(context: Context): Boolean =
        ContextCompat.checkSelfPermission(context, PERMISSION) == PackageManager.PERMISSION_GRANTED

    /** Runs *command* in Termux in the background. */
    fun run(context: Context, command: String): Result {
        if (!isInstalled(context)) return Result.TermuxMissing
        if (!hasPermission(context)) return Result.PermissionMissing

        val intent = Intent().apply {
            setClassName(PACKAGE, SERVICE)
            action = ACTION
            putExtra("com.termux.RUN_COMMAND_PATH", BASH)
            // -lc so the login shell sets PATH and mpvbridge is found however it was installed.
            putExtra("com.termux.RUN_COMMAND_ARGUMENTS", arrayOf("-lc", command))
            putExtra("com.termux.RUN_COMMAND_WORKDIR", HOME)
            putExtra("com.termux.RUN_COMMAND_BACKGROUND", true)
            putExtra("com.termux.RUN_COMMAND_SESSION_ACTION", "0")
        }

        return try {
            ContextCompat.startForegroundService(context, intent)
            Log.i(TAG, "asked Termux to run: $command")
            Result.Started
        } catch (exc: Exception) {
            Log.w(TAG, "Termux refused the command", exc)
            Result.Failed(exc.message ?: exc.javaClass.simpleName)
        }
    }
}
