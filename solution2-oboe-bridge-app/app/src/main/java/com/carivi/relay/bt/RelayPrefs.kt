package com.carivi.relay.bt

import android.content.Context
import android.content.SharedPreferences
import com.carivi.relay.audiopatch.AudioPatchBridge
import com.carivi.relay.core.RelayMode

/** Persisted dashboard choices (device roles, architecture, toggles). */
class RelayPrefs(context: Context) {
    private val sp: SharedPreferences =
        context.createDeviceProtectedStorageContext()
            .getSharedPreferences("carrelay", Context.MODE_PRIVATE)

    var iphoneAddress: String?
        get() = sp.getString(KEY_IPHONE, null)
        set(v) = sp.edit().putString(KEY_IPHONE, v).apply()

    var carAddress: String?
        get() = sp.getString(KEY_CAR, null)
        set(v) = sp.edit().putString(KEY_CAR, v).apply()

    var mode: RelayMode
        get() = RelayMode.fromOrdinalOrDefault(sp.getInt(KEY_MODE, RelayMode.NATIVE_FOCUS.ordinal))
        set(v) = sp.edit().putInt(KEY_MODE, v.ordinal).apply()

    var patchStrategy: AudioPatchBridge.Strategy
        get() = AudioPatchBridge.Strategy.entries.getOrElse(
            sp.getInt(KEY_PATCH_STRATEGY, 0)
        ) { AudioPatchBridge.Strategy.DEVICE_PATCH }
        set(v) = sp.edit().putInt(KEY_PATCH_STRATEGY, v.ordinal).apply()

    /** Mode 2: skip the HAL "a2dp input" device and go straight to the UID loop-back feed. */
    var forceLoopback: Boolean
        get() = sp.getBoolean(KEY_FORCE_LOOPBACK, false)
        set(v) = sp.edit().putBoolean(KEY_FORCE_LOOPBACK, v).apply()

    /** Run the connection sequencer automatically on ACTION_POWER_CONNECTED. */
    var autoSequenceOnPower: Boolean
        get() = sp.getBoolean(KEY_AUTO_POWER, true)
        set(v) = sp.edit().putBoolean(KEY_AUTO_POWER, v).apply()

    /** Also connect HFP client (calls) to the iPhone in the sequencer. */
    var hfpClientEnabled: Boolean
        get() = sp.getBoolean(KEY_HFP_CLIENT, true)
        set(v) = sp.edit().putBoolean(KEY_HFP_CLIENT, v).apply()

    /** Allow the tablet to act as HFP AG for the car (off keeps the radio quieter). */
    var carHfpAgEnabled: Boolean
        get() = sp.getBoolean(KEY_CAR_HFP_AG, false)
        set(v) = sp.edit().putBoolean(KEY_CAR_HFP_AG, v).apply()

    val hasEndpoints: Boolean get() = !iphoneAddress.isNullOrBlank() && !carAddress.isNullOrBlank()

    companion object {
        private const val KEY_IPHONE = "iphone_address"
        private const val KEY_CAR = "car_address"
        private const val KEY_MODE = "relay_mode"
        private const val KEY_PATCH_STRATEGY = "patch_strategy"
        private const val KEY_FORCE_LOOPBACK = "force_loopback"
        private const val KEY_AUTO_POWER = "auto_sequence_on_power"
        private const val KEY_HFP_CLIENT = "hfp_client_enabled"
        private const val KEY_CAR_HFP_AG = "car_hfp_ag_enabled"
    }
}
