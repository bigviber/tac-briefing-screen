package com.carivi.relay.engine

import android.content.Context
import android.util.Log
import com.carivi.relay.audio.AudioDeviceResolver
import com.carivi.relay.bt.RelayPrefs
import com.carivi.relay.core.EngineStats
import com.carivi.relay.core.EngineStatus
import com.carivi.relay.core.RelayEndpoints
import com.carivi.relay.core.RelayEngine
import com.carivi.relay.core.RelayMode
import com.carivi.relay.core.audio.A2dpInputPublisher
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext

/**
 * Mode 2 - Google Oboe full-duplex C++ bridge (libcarrelay_bridge.so).
 *
 * Preferred path: capture from the AudioDeviceInfo.TYPE_BLUETOOTH_A2DP *input*
 * device (HAL module "a2dp", mixPort "a2dp input") after publishing it via
 * AudioSystem.setDeviceConnectionState, and write to the car's
 * TYPE_BLUETOOTH_A2DP output device id, SharingMode::Shared +
 * PerformanceMode::LowLatency so local Maps audio mixes in.
 *
 * Fallback (or forced via prefs.forceLoopback): a uid-1002 AudioPolicy loop-back
 * mix feeds the same Oboe output stream in push mode. Needed when the QTI stack
 * plays the decoded sink stream as an AudioTrack instead of handing PCM to the
 * a2dp HAL input (the stock behaviour of the R stack).
 */
class OboeBridgeEngine(
    private val context: Context,
    private val resolver: AudioDeviceResolver,
    private val prefs: RelayPrefs,
    private val publisher: A2dpInputPublisher = A2dpInputPublisher(context),
    private val loopback: LoopbackCaptureSource = LoopbackCaptureSource(context)
) : RelayEngine {

    private val tag = "CarRelay.OboeEngine"
    private val lock = Mutex()
    @Volatile private var status: EngineStatus = EngineStatus.Idle
    @Volatile private var ingress: String = ""

    override val mode: RelayMode = RelayMode.OBOE_BRIDGE

    override suspend fun start(endpoints: RelayEndpoints): EngineStatus = lock.withLock {
        withContext(Dispatchers.IO) {
            stopLocked()
            status = EngineStatus.Starting("resolving car output")

            val car = resolver.carOutput(endpoints.carAddress)
            if (car == null) {
                status = EngineStatus.Failed("car A2DP output device not present - run the connection sequencer first")
                return@withContext status
            }
            val outRate = resolver.preferredRate(car, 48000)
            val carLabel = "${car.productName} (#${car.id}, ${outRate} Hz)"

            if (!prefs.forceLoopback) {
                status = EngineStatus.Starting("publishing BT A2DP In")
                if (publisher.publish(endpoints.iphoneAddress)) {
                    delay(350) // let APM open the a2dp input on module "a2dp"
                    val input = resolver.a2dpInput(endpoints.iphoneAddress)
                    if (input != null) {
                        val rc = NativeBridgeLib.startDuplex(input.id, car.id, outRate, 2)
                        if (rc == NativeBridgeLib.RESULT_OK) {
                            ingress = "HAL a2dp input #${input.id}"
                            status = EngineStatus.Running("Oboe duplex: $ingress → $carLabel")
                            return@withContext status
                        }
                        Log.w(tag, "startDuplex failed rc=$rc (${NativeBridgeLib.lastError()}), falling back to loop-back")
                    } else {
                        Log.w(tag, "no TYPE_BLUETOOTH_A2DP input device after publish, falling back to loop-back")
                    }
                } else {
                    Log.w(tag, "publish(BT A2DP In) refused, falling back to loop-back")
                }
                publisher.unpublish()
            }

            status = EngineStatus.Starting("starting Oboe output + uid loop-back")
            val rc = NativeBridgeLib.startPush(car.id, 48000, 2)
            if (rc != NativeBridgeLib.RESULT_OK) {
                status = EngineStatus.Failed("Oboe output open failed rc=$rc ${NativeBridgeLib.lastError()}")
                return@withContext status
            }
            val ok = loopback.start { buf, frames -> NativeBridgeLib.writePcm(buf, frames) }
            if (!ok) {
                NativeBridgeLib.stop()
                status = EngineStatus.Failed("AudioPolicy loop-back mix failed (MODIFY_AUDIO_ROUTING / RECORD_AUDIO?)")
                return@withContext status
            }
            ingress = loopback.description
            status = if (prefs.forceLoopback) {
                EngineStatus.Running("Oboe push: $ingress → $carLabel")
            } else {
                EngineStatus.Degraded("HAL a2dp input silent; Oboe push: $ingress → $carLabel")
            }
            status
        }
    }

    override suspend fun stop() = lock.withLock {
        withContext(Dispatchers.IO) { stopLocked() }
    }

    private fun stopLocked() {
        loopback.stop()
        NativeBridgeLib.stop()
        publisher.unpublish()
        ingress = ""
        status = EngineStatus.Idle
    }

    override fun stats(): EngineStats {
        val s = NativeBridgeLib.stats()
        return if (loopback.isRunning) {
            s.copy(extra = s.extra + " captured=${loopback.framesCaptured}f")
        } else s
    }

    override fun status(): EngineStatus {
        val st = status
        if (st is EngineStatus.Running || st is EngineStatus.Degraded) {
            if (!NativeBridgeLib.isRunning()) {
                return EngineStatus.Failed("Oboe engine stopped: ${NativeBridgeLib.lastError()}")
            }
        }
        return st
    }
}
