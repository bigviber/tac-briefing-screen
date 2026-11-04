package com.carivi.relay.audio

import android.content.Context
import android.media.AudioDeviceCallback
import android.media.AudioDeviceInfo
import android.media.AudioManager
import android.os.Handler
import android.os.Looper

/**
 * Maps the Bluetooth MAC addresses the user pinned on the dashboard to
 * AudioDeviceInfo ids, which is what Oboe (setDeviceId) and the AudioPolicy
 * render mix (setDevice) need.
 */
class AudioDeviceResolver(context: Context) {
    private val audioManager = context.getSystemService(AudioManager::class.java)

    /** Car stereo output: TYPE_BLUETOOTH_A2DP sink whose address matches (or the only one). */
    fun carOutput(carAddress: String?): AudioDeviceInfo? {
        val outs = audioManager.getDevices(AudioManager.GET_DEVICES_OUTPUTS)
            .filter { it.type == AudioDeviceInfo.TYPE_BLUETOOTH_A2DP }
        if (outs.isEmpty()) return null
        return outs.firstOrNull { carAddress != null && it.address.equals(carAddress, ignoreCase = true) }
            ?: outs.singleOrNull()
            ?: outs.first()
    }

    /** iPhone sink link seen as an input device (only after A2dpInputPublisher.publish()). */
    fun a2dpInput(iphoneAddress: String?): AudioDeviceInfo? {
        val ins = audioManager.getDevices(AudioManager.GET_DEVICES_INPUTS)
            .filter { it.type == AudioDeviceInfo.TYPE_BLUETOOTH_A2DP }
        return ins.firstOrNull { iphoneAddress != null && it.address.equals(iphoneAddress, ignoreCase = true) }
            ?: ins.firstOrNull()
    }

    fun preferredRate(device: AudioDeviceInfo?, fallback: Int = 48000): Int {
        val rates = device?.sampleRates ?: return fallback
        if (rates.isEmpty() || rates.contains(fallback)) return fallback
        return rates.max()
    }

    /** One line per device for the diagnostics card. */
    fun describe(): List<String> {
        val out = audioManager.getDevices(AudioManager.GET_DEVICES_OUTPUTS).map { "OUT #${it.id} ${typeName(it.type)} '${it.productName}' ${it.address} rates=${it.sampleRates.joinToString("/")}" }
        val inp = audioManager.getDevices(AudioManager.GET_DEVICES_INPUTS).map { "IN  #${it.id} ${typeName(it.type)} '${it.productName}' ${it.address} rates=${it.sampleRates.joinToString("/")}" }
        return out + inp
    }

    fun registerCallback(onChanged: () -> Unit): AudioDeviceCallback {
        val cb = object : AudioDeviceCallback() {
            override fun onAudioDevicesAdded(addedDevices: Array<out AudioDeviceInfo>) = onChanged()
            override fun onAudioDevicesRemoved(removedDevices: Array<out AudioDeviceInfo>) = onChanged()
        }
        audioManager.registerAudioDeviceCallback(cb, Handler(Looper.getMainLooper()))
        return cb
    }

    fun unregisterCallback(cb: AudioDeviceCallback) = audioManager.unregisterAudioDeviceCallback(cb)

    companion object {
        fun typeName(type: Int): String = when (type) {
            AudioDeviceInfo.TYPE_BLUETOOTH_A2DP -> "BT_A2DP"
            AudioDeviceInfo.TYPE_BLUETOOTH_SCO -> "BT_SCO"
            AudioDeviceInfo.TYPE_BUILTIN_SPEAKER -> "SPEAKER"
            AudioDeviceInfo.TYPE_BUILTIN_MIC -> "MIC"
            AudioDeviceInfo.TYPE_TELEPHONY -> "TELEPHONY"
            AudioDeviceInfo.TYPE_REMOTE_SUBMIX -> "SUBMIX"
            AudioDeviceInfo.TYPE_USB_DEVICE -> "USB"
            AudioDeviceInfo.TYPE_USB_HEADSET -> "USB_HS"
            AudioDeviceInfo.TYPE_WIRED_HEADSET -> "WIRED_HS"
            AudioDeviceInfo.TYPE_WIRED_HEADPHONES -> "WIRED_HP"
            AudioDeviceInfo.TYPE_FM_TUNER -> "FM"
            else -> "type#$type"
        }
    }
}
