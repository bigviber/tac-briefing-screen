package com.carivi.relay.engine

import com.carivi.relay.core.EngineStats

/**
 * Kotlin side of libcarrelay_bridge.so (Google Oboe full-duplex engine).
 * All entry points are static; the native side keeps one bridge instance.
 */
object NativeBridgeLib {
    init {
        System.loadLibrary("carrelay_bridge")
    }

    /** oboe::Result::OK */
    const val RESULT_OK = 0

    private object StatsIndex {
        const val IN_RATE = 0
        const val OUT_RATE = 1
        const val CHANNELS = 2
        const val IN_XRUNS = 3
        const val OUT_XRUNS = 4
        const val RESTARTS = 5
        const val RING_FILL = 6
        const val SLIPS = 7
        const val LATENCY_US = 8
        const val UNDERRUNS = 9
        const val RUNNING = 10
        const val PUSH_MODE = 11
    }

    /**
     * Full duplex: capture from [inputDeviceId] (AudioDeviceInfo.TYPE_BLUETOOTH_A2DP
     * input) and render to [outputDeviceId] (car TYPE_BLUETOOTH_A2DP output).
     */
    @JvmStatic external fun nativeStartDuplex(inputDeviceId: Int, outputDeviceId: Int, sampleRate: Int, channelCount: Int): Int

    /** Output only; PCM arrives via [nativeWritePcm] (AudioPolicy loop-back feed). */
    @JvmStatic external fun nativeStartPush(outputDeviceId: Int, sampleRate: Int, channelCount: Int): Int

    @JvmStatic external fun nativeStop()

    /** Interleaved 16-bit PCM, returns frames accepted. */
    @JvmStatic external fun nativeWritePcm(buffer: ShortArray, frames: Int): Int

    @JvmStatic external fun nativeGetStats(): LongArray
    @JvmStatic external fun nativeIsRunning(): Boolean
    @JvmStatic external fun nativeLastError(): String

    fun startDuplex(inputDeviceId: Int, outputDeviceId: Int, sampleRate: Int, channelCount: Int): Int =
        nativeStartDuplex(inputDeviceId, outputDeviceId, sampleRate, channelCount)

    fun startPush(outputDeviceId: Int, sampleRate: Int, channelCount: Int): Int =
        nativeStartPush(outputDeviceId, sampleRate, channelCount)

    fun stop() = nativeStop()

    fun writePcm(buffer: ShortArray, frames: Int): Int = nativeWritePcm(buffer, frames)

    fun isRunning(): Boolean = nativeIsRunning()

    fun lastError(): String = nativeLastError()

    fun stats(): EngineStats {
        val a = nativeGetStats()
        if (a.size < 12) return EngineStats()
        val pushMode = a[StatsIndex.PUSH_MODE] == 1L
        return EngineStats(
            inputSampleRate = a[StatsIndex.IN_RATE].toInt(),
            outputSampleRate = a[StatsIndex.OUT_RATE].toInt(),
            channelCount = a[StatsIndex.CHANNELS].toInt(),
            inputXRuns = a[StatsIndex.IN_XRUNS].toInt(),
            outputXRuns = a[StatsIndex.OUT_XRUNS].toInt(),
            restarts = a[StatsIndex.RESTARTS].toInt(),
            bufferFillFrames = a[StatsIndex.RING_FILL].toInt(),
            slipCorrections = a[StatsIndex.SLIPS],
            latencyMs = a[StatsIndex.LATENCY_US] / 1000.0,
            extra = buildString {
                append(if (pushMode) "push(loopback)" else "duplex(HAL a2dp in)")
                append(" underruns=").append(a[StatsIndex.UNDERRUNS])
                if (a[StatsIndex.RUNNING] != 1L) append(" STOPPED")
            }
        )
    }
}
