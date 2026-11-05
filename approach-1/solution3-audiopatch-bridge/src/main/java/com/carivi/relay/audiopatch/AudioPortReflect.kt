package com.carivi.relay.audiopatch

import android.util.Log
import com.carivi.relay.core.reflect.AudioSystemReflect
import com.carivi.relay.core.reflect.Hidden
import org.joor.Reflect

/**
 * Reflective view of AudioFlinger's port/patch API (android.media.AudioPort,
 * AudioDevicePort, AudioPortConfig, AudioPatch, AudioSystem.createAudioPatch),
 * all of which are hidden on SDK 30.
 */
object AudioPortReflect {
    private const val TAG = "CarRelay.Ports"

    const val CLS_AUDIO_MANAGER = "android.media.AudioManager"
    const val CLS_AUDIO_PORT = "android.media.AudioPort"
    const val CLS_AUDIO_DEVICE_PORT = "android.media.AudioDevicePort"
    const val CLS_AUDIO_PORT_CONFIG = "android.media.AudioPortConfig"
    const val CLS_AUDIO_PATCH = "android.media.AudioPatch"

    // AudioPort.ROLE_*
    const val ROLE_NONE = 0
    const val ROLE_SOURCE = 1
    const val ROLE_SINK = 2

    /** Flattened description of a device port. */
    data class DevicePort(
        val raw: Any,
        val id: Int,
        val role: Int,
        val type: Int,
        val address: String,
        val name: String,
        val samplingRates: IntArray,
        val channelMasks: IntArray,
        val formats: IntArray
    ) {
        val isSource: Boolean get() = role == ROLE_SOURCE
        val isSink: Boolean get() = role == ROLE_SINK
        val typeName: String get() = AudioSystemReflect.deviceName(type)

        fun preferredRate(preferred: Int = 48000): Int = when {
            samplingRates.isEmpty() -> preferred
            samplingRates.contains(preferred) -> preferred
            else -> samplingRates.max()
        }

        override fun toString(): String =
            "DevicePort(id=$id role=$role type=$typeName addr='$address' name='$name' rates=${samplingRates.joinToString("/")})"
    }

    /** Snapshot of every device port AudioFlinger currently exposes. */
    fun listDevicePorts(): List<DevicePort> {
        val ports = ArrayList<Any>()
        val status = Hidden.callStatic<Int>(CLS_AUDIO_MANAGER, "listAudioPorts", ports)
        if (status !is Hidden.Outcome.Ok || status.value != 0) {
            Log.w(TAG, "listAudioPorts failed: ${status.describe()}")
            return emptyList()
        }
        val devicePortClass = Hidden.classOrNull(CLS_AUDIO_DEVICE_PORT) ?: return emptyList()
        return ports.filter { devicePortClass.isInstance(it) }.mapNotNull { p ->
            try {
                val r = Reflect.on(p)
                DevicePort(
                    raw = p,
                    id = r.call("id").get(),
                    role = r.call("role").get(),
                    type = r.call("type").get(),
                    address = r.call("address").get<String?>() ?: "",
                    name = r.call("name").get<String?>() ?: "",
                    samplingRates = r.call("samplingRates").get<IntArray?>() ?: IntArray(0),
                    channelMasks = r.call("channelMasks").get<IntArray?>() ?: IntArray(0),
                    formats = r.call("formats").get<IntArray?>() ?: IntArray(0)
                )
            } catch (t: Throwable) {
                Log.w(TAG, "could not read port $p", t)
                null
            }
        }
    }

    /** AudioDevicePort.buildConfig(int samplingRate, int channelMask, int format, AudioGainConfig gain) */
    fun buildConfig(port: DevicePort, samplingRate: Int, channelMask: Int, format: Int): Any? =
        Hidden.call<Any>(port.raw, "buildConfig", samplingRate, channelMask, format, null).valueOrNull()

    /**
     * AudioSystem.createAudioPatch(AudioPatch[] patch, AudioPortConfig[] sources, AudioPortConfig[] sinks)
     * Returns the created AudioPatch object or null; status is logged.
     */
    fun createAudioPatch(sourceCfg: Any, sinkCfg: Any): Pair<Int, Any?> {
        val patchClass = Hidden.classOrNull(CLS_AUDIO_PATCH) ?: return -1 to null
        val cfgClass = Hidden.classOrNull(CLS_AUDIO_PORT_CONFIG) ?: return -1 to null

        val patchArr = java.lang.reflect.Array.newInstance(patchClass, 1)
        val srcArr = java.lang.reflect.Array.newInstance(cfgClass, 1).also {
            java.lang.reflect.Array.set(it, 0, sourceCfg)
        }
        val sinkArr = java.lang.reflect.Array.newInstance(cfgClass, 1).also {
            java.lang.reflect.Array.set(it, 0, sinkCfg)
        }
        val status = Hidden.callStatic<Int>(AudioSystemReflect.CLASS, "createAudioPatch", patchArr, srcArr, sinkArr)
        val code = status.valueOrNull() ?: -1
        val patch = if (code == 0) java.lang.reflect.Array.get(patchArr, 0) else null
        Log.i(TAG, "createAudioPatch -> $code patch=$patch")
        return code to patch
    }

    /** AudioSystem.releaseAudioPatch(AudioPatch) */
    fun releaseAudioPatch(patch: Any): Int =
        Hidden.callStatic<Int>(AudioSystemReflect.CLASS, "releaseAudioPatch", patch).valueOrNull() ?: -1

    /** Patch handle id for display. */
    fun patchId(patch: Any): Int = Hidden.call<Int>(patch, "id").valueOrNull() ?: -1
}
