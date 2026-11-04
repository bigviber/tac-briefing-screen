package com.carivi.relay.root

import android.content.Context
import android.content.pm.PackageManager
import com.carivi.relay.core.BtConstants

/**
 * Runtime activation of the Solution 1 stack from inside the app (mirrors what
 * service.sh does at boot, for the case where the Magisk module was updated
 * without a reboot or someone toggled a service by hand).
 */
class SystemBootstrap(private val context: Context) {

    companion object {
        const val NEW_OVERLAY = "com.carivi.bt.overlay"
        const val OLD_OVERLAY = "com.local.btsink.overlay"
        private val SINK_SIDE_SERVICES = listOf(
            "a2dpsink.A2dpSinkService",
            "avrcpcontroller.AvrcpControllerService",
            "avrcpcontroller.BluetoothMediaBrowserService",
            "hfpclient.HeadsetClientService",
            "pbapclient.PbapClientService"
        )
        private val OVERLAY_KEYS = listOf(
            "bool/profile_supported_a2dp_sink",
            "bool/profile_supported_avrcp_controller",
            "bool/profile_supported_hfpclient",
            "bool/a2dp_sink_automatically_request_audio_focus"
        )
    }

    /** True when PackageManager actually granted the privileged permissions (priv-app + whitelist OK). */
    fun isPrivileged(): Boolean {
        val pm = context.packageManager
        val pkg = context.packageName
        return pm.checkPermission("android.permission.BLUETOOTH_PRIVILEGED", pkg) == PackageManager.PERMISSION_GRANTED &&
            pm.checkPermission("android.permission.MODIFY_AUDIO_ROUTING", pkg) == PackageManager.PERMISSION_GRANTED
    }

    /** Apply overlays + component states + hidden API policy. Returns log lines. */
    suspend fun apply(): List<String> {
        val log = ArrayList<String>()
        if (!RootShell.isRootAvailable()) {
            log += "root shell unavailable - bootstrap skipped (service.sh covers boot)"
            return log
        }
        val bt = BtConstants.BLUETOOTH_PACKAGE
        val cmds = buildList {
            add("settings put global hidden_api_policy 1")
            add("cmd overlay enable --user 0 $OLD_OVERLAY")
            add("cmd overlay enable --user 0 $NEW_OVERLAY")
            add("cmd overlay set-priority $NEW_OVERLAY highest")
            SINK_SIDE_SERVICES.forEach { add("pm enable --user 0 $bt/.$it") }
            add("pm disable --user 0 $bt/.avrcp.AvrcpTargetService")
            add("pm grant ${context.packageName} android.permission.RECORD_AUDIO")
            add("dumpsys deviceidle whitelist +${context.packageName}")
        }
        for (c in cmds) {
            val r = RootShell.run(c)
            log += (if (r.ok) "✓ " else "✗ ") + c + (if (r.text.isNotBlank()) " → ${r.text.lines().first()}" else "")
        }
        log += overlayLookup()
        return log
    }

    /** Resolve the effective Bluetooth resource values (shows whether the RRO won). */
    suspend fun overlayLookup(): List<String> {
        if (!RootShell.isRootAvailable()) return listOf("overlay lookup needs root")
        val bt = BtConstants.BLUETOOTH_PACKAGE
        return OVERLAY_KEYS.map { key ->
            val r = RootShell.run("cmd overlay lookup $bt $bt:$key")
            "$key = ${r.text.trim().ifEmpty { "?" }}"
        }
    }

    /** Short diagnostics dump for the dashboard's log card. */
    suspend fun diagnostics(): List<String> {
        if (!RootShell.isRootAvailable()) return listOf("diagnostics need root")
        val out = ArrayList<String>()
        out += RootShell.run("getprop | grep -E 'persist\\.(vendor\\.)?(bluetooth|qcom\\.bluetooth|bt\\.|vendor\\.service\\.bt)'").out
        out += RootShell.run("dumpsys bluetooth_manager | grep -E 'MaxConnectedAudioDevices|A2dpOffloadEnabled|Profile: (A2dpService|A2dpSinkService|AvrcpControllerService|HeadsetClientService)|mActiveDevice|Devices Tracked'").out
        out += RootShell.run("dumpsys media.audio_policy | grep -E 'Config source|BT A2DP (In|Out)|AUDIO_DEVICE_(IN|OUT)_BLUETOOTH_A2DP' | sort -u").out
        out += RootShell.run("grep audio_policy_configuration /proc/mounts || echo 'policy: stock file (no bind mount)'").out
        return out
    }
}
