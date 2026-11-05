package com.carivi.relay.engine

import android.content.Context
import android.media.AudioRecord
import android.os.Process
import android.util.Log
import com.carivi.relay.audiopatch.UidAudioPolicy

/**
 * PCM ingress for Mode 2 when the HAL "a2dp input" device yields no samples:
 * a dynamic AudioPolicy loop-back mix scoped to uid 1002 diverts the Bluetooth
 * process' sink AudioTrack into a submix and hands it back as an AudioRecord.
 * The record thread pushes 16-bit interleaved frames into the Oboe ring buffer.
 *
 * Because the mix is ROUTE_FLAG_LOOP_BACK the original track no longer reaches
 * the speaker/car on its own - there is exactly one copy, the one Oboe renders.
 */
class LoopbackCaptureSource(private val context: Context) {
    private val tag = "CarRelay.Loopback"

    private var registration: UidAudioPolicy.Registration? = null
    private var record: AudioRecord? = null
    private var worker: Thread? = null
    @Volatile private var running = false
    @Volatile var framesCaptured: Long = 0L
        private set
    val description: String get() = registration?.description ?: "loopback inactive"

    fun start(onPcm: (ShortArray, Int) -> Unit): Boolean {
        stop()
        val uid = UidAudioPolicy.bluetoothUid(context)
        val pair = UidAudioPolicy.registerLoopbackMix(context, uid) ?: return false
        val (reg, rec) = pair
        registration = reg
        record = rec

        try {
            rec.startRecording()
        } catch (t: Throwable) {
            Log.w(tag, "startRecording failed", t)
            stop()
            return false
        }
        if (rec.recordingState != AudioRecord.RECORDSTATE_RECORDING) {
            Log.w(tag, "AudioRecord not recording (state=${rec.recordingState}) - RECORD_AUDIO granted?")
            stop()
            return false
        }

        running = true
        framesCaptured = 0
        val channels = 2
        // 10 ms chunks at the mix rate keep the ring buffer fill shallow.
        val frames = UidAudioPolicy.MIX_SAMPLE_RATE / 100
        worker = Thread({
            Process.setThreadPriority(Process.THREAD_PRIORITY_URGENT_AUDIO)
            val buf = ShortArray(frames * channels)
            while (running) {
                val n = rec.read(buf, 0, buf.size, AudioRecord.READ_BLOCKING)
                if (n > 0) {
                    val got = n / channels
                    framesCaptured += got
                    onPcm(buf, got)
                } else if (n < 0) {
                    Log.w(tag, "AudioRecord.read error $n")
                    break
                }
            }
        }, "carrelay-loopback").also { it.start() }
        Log.i(tag, "loopback capture started: ${reg.description}")
        return true
    }

    fun stop() {
        running = false
        worker?.let { t ->
            try { t.join(500) } catch (_: InterruptedException) {}
        }
        worker = null
        record?.let {
            try { it.stop() } catch (_: Throwable) {}
            it.release()
        }
        record = null
        registration?.let { UidAudioPolicy.unregister(context, it) }
        registration = null
    }

    val isRunning: Boolean get() = running
}
