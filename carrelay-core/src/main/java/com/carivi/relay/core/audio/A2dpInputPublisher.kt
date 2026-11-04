package com.carivi.relay.core.audio

import android.content.Context
import android.media.AudioDeviceInfo
import android.media.AudioManager
import android.util.Log
import com.carivi.relay.core.reflect.AudioSystemReflect

/**
 * Makes the policy-declared "BT A2DP In" device (AUDIO_DEVICE_IN_BLUETOOTH_A2DP,
 * module "a2dp", mixPort "a2dp input") *available* in AudioPolicyManager.
 *
 * Ground truth: the live /vendor/etc/audio/audio_policy_configuration.xml on
 * S320311_230810_ROW declares the port and the route (HW Module 2, handle 18),
 * but "Available input devices" in dumpsys media.audio_policy never lists it,
 * because A2dpSinkService on SDK 30 does not report the sink link to
 * AudioService. Mode 2 (Oboe capture) and Mode 3 (AudioPatch) need the device
 * to be available before an input stream / patch can target it, so the relay
 * publishes it itself through AudioSystem.setDeviceConnectionState
 * (MODIFY_AUDIO_ROUTING / MODIFY_AUDIO_SETTINGS, which the priv-app holds).
 */
class A2dpInputPublisher(private val context: Context) {
    private val tag = "CarRelay.A2dpIn"
    private val audioManager = context.getSystemService(AudioManager::class.java)
    private var publishedAddress: String? = null

    /** Publish the sink link as an available input device. Returns true on success. */
    fun publish(iphoneAddress: String, deviceName: String = "iPhone A2DP"): Boolean {
        val status = AudioSystemReflect.setDeviceConnectionState(
            AudioSystemReflect.DEVICE_IN_BLUETOOTH_A2DP,
            AudioSystemReflect.DEVICE_STATE_AVAILABLE,
            iphoneAddress,
            deviceName
        )
        Log.i(tag, "setDeviceConnectionState(IN_BLUETOOTH_A2DP, AVAILABLE, $iphoneAddress) -> $status")
        if (status == AudioSystemReflect.SUCCESS) publishedAddress = iphoneAddress
        return status == AudioSystemReflect.SUCCESS
    }

    /** Withdraw the device again (idempotent). */
    fun unpublish() {
        val addr = publishedAddress ?: return
        val status = AudioSystemReflect.setDeviceConnectionState(
            AudioSystemReflect.DEVICE_IN_BLUETOOTH_A2DP,
            AudioSystemReflect.DEVICE_STATE_UNAVAILABLE,
            addr,
            "iPhone A2DP"
        )
        Log.i(tag, "setDeviceConnectionState(IN_BLUETOOTH_A2DP, UNAVAILABLE, $addr) -> $status")
        publishedAddress = null
    }

    fun isPublished(iphoneAddress: String): Boolean =
        AudioSystemReflect.getDeviceConnectionState(
            AudioSystemReflect.DEVICE_IN_BLUETOOTH_A2DP, iphoneAddress
        ) == AudioSystemReflect.DEVICE_STATE_AVAILABLE

    /** The framework view of the input device once published, or null. */
    fun findInputDevice(): AudioDeviceInfo? =
        audioManager.getDevices(AudioManager.GET_DEVICES_INPUTS)
            .firstOrNull { it.type == AudioDeviceInfo.TYPE_BLUETOOTH_A2DP }
}
