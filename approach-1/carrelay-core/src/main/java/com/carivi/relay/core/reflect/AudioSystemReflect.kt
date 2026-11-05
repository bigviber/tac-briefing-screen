package com.carivi.relay.core.reflect

import android.media.AudioFormat
import android.util.Log

/**
 * Reflective access to android.media.AudioSystem (hidden on SDK 30).
 * Only the handful of entry points the three architectures need.
 */
object AudioSystemReflect {
    private const val TAG = "CarRelay.AudioSystem"
    const val CLASS = "android.media.AudioSystem"

    // audio_devices_t values (system/media/audio/include/system/audio-base.h, R)
    const val DEVICE_BIT_IN = 0x80000000.toInt()
    val DEVICE_OUT_BLUETOOTH_A2DP: Int = Hidden.staticField(CLASS, "DEVICE_OUT_BLUETOOTH_A2DP", 0x80)
    val DEVICE_OUT_BLUETOOTH_A2DP_HEADPHONES: Int =
        Hidden.staticField(CLASS, "DEVICE_OUT_BLUETOOTH_A2DP_HEADPHONES", 0x100)
    val DEVICE_OUT_BLUETOOTH_A2DP_SPEAKER: Int =
        Hidden.staticField(CLASS, "DEVICE_OUT_BLUETOOTH_A2DP_SPEAKER", 0x200)
    val DEVICE_IN_BLUETOOTH_A2DP: Int =
        Hidden.staticField(CLASS, "DEVICE_IN_BLUETOOTH_A2DP", DEVICE_BIT_IN or 0x20000)

    const val DEVICE_STATE_UNAVAILABLE = 0
    const val DEVICE_STATE_AVAILABLE = 1

    /** AUDIO_FORMAT_DEFAULT: let the HAL choose the codec/format. */
    const val AUDIO_FORMAT_DEFAULT = 0

    const val SUCCESS = 0

    /**
     * AudioSystem.setDeviceConnectionState(int device, int state, String address,
     * String name, int codecFormat)  - R signature.
     * Older signature without codecFormat is tried as a fallback.
     */
    fun setDeviceConnectionState(
        device: Int,
        state: Int,
        address: String,
        name: String,
        codecFormat: Int = AUDIO_FORMAT_DEFAULT
    ): Int {
        val r = Hidden.callStatic<Int>(CLASS, "setDeviceConnectionState", device, state, address, name, codecFormat)
        if (r is Hidden.Outcome.Ok) return r.value
        val legacy = Hidden.callStatic<Int>(CLASS, "setDeviceConnectionState", device, state, address, name)
        return legacy.valueOrNull() ?: -1
    }

    /** AudioSystem.getDeviceConnectionState(int device, String address) */
    fun getDeviceConnectionState(device: Int, address: String): Int =
        Hidden.callStatic<Int>(CLASS, "getDeviceConnectionState", device, address).valueOrNull() ?: -1

    /**
     * Human readable name for an audio_devices_t value (debug cards).
     * Mirrors AudioSystem.getOutputDeviceName / getInputDeviceName when present.
     */
    fun deviceName(device: Int): String {
        val isInput = device and DEVICE_BIT_IN != 0
        val method = if (isInput) "getInputDeviceName" else "getOutputDeviceName"
        return Hidden.callStatic<String>(CLASS, method, device).valueOrNull()
            ?: "0x" + Integer.toHexString(device)
    }

    /** AudioFormat channel masks for patch configs. */
    const val CHANNEL_OUT_STEREO = AudioFormat.CHANNEL_OUT_STEREO
    const val CHANNEL_IN_STEREO = AudioFormat.CHANNEL_IN_STEREO
    const val ENCODING_PCM_16BIT = AudioFormat.ENCODING_PCM_16BIT

    fun log(msg: String) = Log.i(TAG, msg)
}
