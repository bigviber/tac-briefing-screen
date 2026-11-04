package com.carivi.relay.root

import android.util.Log
import com.topjohnwu.superuser.Shell
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/**
 * libsu wrapper. Privileged commands (cmd overlay, pm enable, settings put,
 * dumpsys) run through a persistent root shell; everything is logged.
 */
object RootShell {
    private const val TAG = "CarRelay.Root"

    data class CommandResult(val ok: Boolean, val code: Int, val out: List<String>, val err: List<String>) {
        val text: String get() = (out + err).joinToString("\n")
    }

    /** Called from CarRelayApp's static initialiser, before any Shell use. */
    fun configure(debug: Boolean) {
        Shell.enableVerboseLogging = debug
        Shell.setDefaultBuilder(
            Shell.Builder.create()
                .setFlags(Shell.FLAG_REDIRECT_STDERR or Shell.FLAG_MOUNT_MASTER)
                .setTimeout(15)
        )
    }

    fun isRootAvailable(): Boolean = try {
        Shell.getShell().isRoot
    } catch (t: Throwable) {
        Log.w(TAG, "root shell unavailable", t)
        false
    }

    suspend fun run(vararg commands: String): CommandResult = withContext(Dispatchers.IO) {
        try {
            val r = Shell.cmd(*commands).exec()
            val res = CommandResult(r.isSuccess, r.code, r.out, r.err)
            Log.i(TAG, "$ ${commands.joinToString(" && ")} -> ${r.code}")
            res
        } catch (t: Throwable) {
            Log.w(TAG, "shell failed: ${commands.joinToString(" && ")}", t)
            CommandResult(false, -1, emptyList(), listOf(t.toString()))
        }
    }

    /** Fire-and-forget variant for boot-time housekeeping. */
    fun submit(vararg commands: String) {
        Shell.cmd(*commands).submit { r -> Log.i(TAG, "$ ${commands.joinToString(" && ")} -> ${r.code}") }
    }
}
