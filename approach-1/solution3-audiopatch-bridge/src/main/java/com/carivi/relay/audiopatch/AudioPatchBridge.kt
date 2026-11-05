package com.carivi.relay.audiopatch

import android.content.Context
import android.media.AudioDeviceInfo
import android.media.AudioFormat
import android.media.AudioManager
import android.util.Log
import com.carivi.relay.core.EngineStats
import com.carivi.relay.core.EngineStatus
import com.carivi.relay.core.RelayEndpoints
import com.carivi.relay.core.RelayEngine
import com.carivi.relay.core.RelayMode
import com.carivi.relay.core.audio.A2dpInputPublisher
import com.carivi.relay.core.reflect.AudioSystemReflect
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext

/**
 * Solution Architecture 3 - AudioFlinger-side bridge, no PCM through our process.
 *
 * Strategy [Strategy.DEVICE_PATCH] (primary):
 *   AudioSystem.createAudioPatch( BT A2DP In (module "a2dp") -> BT A2DP Out (module "primary") ).
 *   The two device ports live on different HW modules, so AudioPolicyManager
 *   asks AudioFlinger's PatchPanel for a *software* bridge: a PatchRecord on the
 *   a2dp input thread feeding a PatchTrack on the A2DP output thread. That is a
 *   kernel/HAL-level copy inside audioserver; nothing crosses JNI.
 *   Requires the "BT A2DP In" HAL input to actually deliver the decoded PCM.
 *
 * Strategy [Strategy.UID_RENDER_MIX] (alternative, same zero-copy property):
 *   Dynamic AudioPolicy that renders every uid-1002 (Bluetooth) player to the
 *   car A2DP output device. Works with the stock sink path that plays the
 *   decoded stream as an AudioTrack, independent of the HAL input device.
 */
class AudioPatchBridge(
    private val context: Context,
    private val publisher: A2dpInputPublisher = A2dpInputPublisher(context)
) : RelayEngine {

    enum class Strategy { DEVICE_PATCH, UID_RENDER_MIX }

    private val tag = "CarRelay.AudioPatch"
    private val audioManager = context.getSystemService(AudioManager::class.java)
    private val lock = Mutex()

    @Volatile var strategy: Strategy = Strategy.DEVICE_PATCH

    @Volatile private var status: EngineStatus = EngineStatus.Idle
    @Volatile private var stats = EngineStats()
    private var patch: Any? = null
    private var renderRegistration: UidAudioPolicy.Registration? = null

    override val mode: RelayMode = RelayMode.AUDIO_PATCH

    override suspend fun start(endpoints: RelayEndpoints): EngineStatus = lock.withLock {
        withContext(Dispatchers.IO) {
            stopLocked()
            status = EngineStatus.Starting("resolving ports")
            status = when (strategy) {
                Strategy.DEVICE_PATCH -> startDevicePatch(endpoints)
                Strategy.UID_RENDER_MIX -> startUidRenderMix(endpoints)
            }
            status
        }
    }

    private fun startDevicePatch(endpoints: RelayEndpoints): EngineStatus {
        if (!publisher.publish(endpoints.iphoneAddress)) {
            return EngineStatus.Failed("setDeviceConnectionState(IN_BLUETOOTH_A2DP) rejected - MODIFY_AUDIO_ROUTING missing?")
        }
        val ports = AudioPortReflect.listDevicePorts()
        Log.i(tag, "ports: ${ports.joinToString("\n")}")

        val source = ports.firstOrNull { it.isSource && it.type == AudioSystemReflect.DEVICE_IN_BLUETOOTH_A2DP }
            ?: return EngineStatus.Failed("no BT A2DP In device port - a2dp HAL module not loaded or device not published")

        val outTypes = setOf(
            AudioSystemReflect.DEVICE_OUT_BLUETOOTH_A2DP,
            AudioSystemReflect.DEVICE_OUT_BLUETOOTH_A2DP_HEADPHONES,
            AudioSystemReflect.DEVICE_OUT_BLUETOOTH_A2DP_SPEAKER
        )
        val sinks = ports.filter { it.isSink && it.type in outTypes }
        val sink = sinks.firstOrNull { it.address.equals(endpoints.carAddress, ignoreCase = true) }
            ?: sinks.firstOrNull()
            ?: return EngineStatus.Failed("no BT A2DP Out device port - car not connected as A2DP sink?")

        val rate = sink.preferredRate(48000)
        val sourceCfg = AudioPortReflect.buildConfig(
            source, source.preferredRate(rate), AudioFormat.CHANNEL_IN_STEREO, AudioFormat.ENCODING_PCM_16BIT
        ) ?: return EngineStatus.Failed("buildConfig(source) failed")
        val sinkCfg = AudioPortReflect.buildConfig(
            sink, rate, AudioFormat.CHANNEL_OUT_STEREO, AudioFormat.ENCODING_PCM_16BIT
        ) ?: return EngineStatus.Failed("buildConfig(sink) failed")

        val (code, created) = AudioPortReflect.createAudioPatch(sourceCfg, sinkCfg)
        if (code != 0 || created == null) {
            publisher.unpublish()
            return EngineStatus.Failed("createAudioPatch returned $code (EINVAL=-22 → route refused, EPERM=-1 → permission)")
        }
        patch = created
        val id = AudioPortReflect.patchId(created)
        stats = EngineStats(
            inputSampleRate = source.preferredRate(rate),
            outputSampleRate = rate,
            channelCount = 2,
            extra = "patch#$id ${source.typeName}→${sink.typeName} ${sink.address}"
        )
        val degraded = !sink.address.equals(endpoints.carAddress, ignoreCase = true)
        return if (degraded) {
            EngineStatus.Degraded("patch#$id created but sink address '${sink.address}' ≠ car ${endpoints.carAddress}")
        } else {
            EngineStatus.Running("AudioFlinger patch#$id @ ${rate}Hz → ${sink.address}")
        }
    }

    private fun startUidRenderMix(endpoints: RelayEndpoints): EngineStatus {
        val carDevice = audioManager.getDevices(AudioManager.GET_DEVICES_OUTPUTS).firstOrNull {
            it.type == AudioDeviceInfo.TYPE_BLUETOOTH_A2DP &&
                (it.address.equals(endpoints.carAddress, ignoreCase = true) || it.address.isEmpty())
        } ?: return EngineStatus.Failed("car A2DP output device not present - connect A2DP source to the car first")

        val uid = UidAudioPolicy.bluetoothUid(context)
        val reg = UidAudioPolicy.registerRenderMix(context, uid, carDevice)
            ?: return EngineStatus.Failed("registerAudioPolicy(render) failed")
        renderRegistration = reg
        stats = EngineStats(
            inputSampleRate = UidAudioPolicy.MIX_SAMPLE_RATE,
            outputSampleRate = carDevice.sampleRates.maxOrNull() ?: 0,
            channelCount = 2,
            extra = reg.description
        )
        return EngineStatus.Running(reg.description)
    }

    override suspend fun stop() = lock.withLock {
        withContext(Dispatchers.IO) { stopLocked() }
    }

    private fun stopLocked() {
        patch?.let {
            val rc = AudioPortReflect.releaseAudioPatch(it)
            Log.i(tag, "releaseAudioPatch -> $rc")
        }
        patch = null
        renderRegistration?.let { UidAudioPolicy.unregister(context, it) }
        renderRegistration = null
        publisher.unpublish()
        status = EngineStatus.Idle
        stats = EngineStats()
    }

    override fun stats(): EngineStats = stats
    override fun status(): EngineStatus = status
}
