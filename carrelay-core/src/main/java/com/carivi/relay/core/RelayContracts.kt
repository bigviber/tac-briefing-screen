package com.carivi.relay.core

/**
 * The three routing architectures the dashboard can switch between at runtime.
 *
 * All three share the same Bluetooth topology (iPhone -> tablet A2DP Sink,
 * tablet A2DP Source -> car). They differ only in *where* the decoded iPhone
 * PCM is moved into the car output:
 *
 *  - [NATIVE_FOCUS]   nothing copies PCM at all. The Bluetooth process plays the
 *                     decoded stream as a media AudioTrack (A2dpSinkStreamHandler);
 *                     AudioPolicyManager routes media to the active A2DP device,
 *                     which the coordinator pins to the car.
 *  - [OBOE_BRIDGE]    Google Oboe full-duplex engine in our process: capture from
 *                     the BT A2DP input device (or a UID-scoped AudioPolicy
 *                     loop-back when the HAL input is silent) and render to the
 *                     car's A2DP output device id with LowLatency/Shared streams.
 *  - [AUDIO_PATCH]    AudioFlinger-side bridge: AudioSystem.createAudioPatch()
 *                     between the BT A2DP In device port and the BT A2DP Out
 *                     device port (software PatchPanel bridge, no JNI copy),
 *                     with a UID render AudioPolicy mix as the alternative.
 */
enum class RelayMode(val title: String, val subtitle: String) {
    NATIVE_FOCUS("Mode 1 · Native focus", "A2dpSinkStreamHandler → APM → car"),
    OBOE_BRIDGE("Mode 2 · Oboe duplex", "C++ FullDuplexStream PCM bridge"),
    AUDIO_PATCH("Mode 3 · AudioPatch", "AudioFlinger HAL direct patch");

    companion object {
        fun fromOrdinalOrDefault(ordinal: Int): RelayMode =
            entries.getOrElse(ordinal) { NATIVE_FOCUS }
    }
}

/** Lifecycle state of a running [RelayEngine]. */
sealed class EngineStatus {
    data object Idle : EngineStatus()
    data class Starting(val step: String) : EngineStatus()
    data class Running(val detail: String) : EngineStatus()
    data class Degraded(val detail: String) : EngineStatus()
    data class Failed(val reason: String) : EngineStatus()

    val isActive: Boolean get() = this is Running || this is Degraded || this is Starting

    fun describe(): String = when (this) {
        Idle -> "idle"
        is Starting -> "starting: $step"
        is Running -> "running: $detail"
        is Degraded -> "degraded: $detail"
        is Failed -> "failed: $reason"
    }
}

/** Live counters surfaced on the dashboard cards. */
data class EngineStats(
    val inputSampleRate: Int = 0,
    val outputSampleRate: Int = 0,
    val channelCount: Int = 0,
    val inputXRuns: Int = 0,
    val outputXRuns: Int = 0,
    val restarts: Int = 0,
    val bufferFillFrames: Int = 0,
    val slipCorrections: Long = 0,
    val latencyMs: Double = 0.0,
    val extra: String = ""
)

/** Addresses the user pinned on the dashboard. */
data class RelayEndpoints(
    /** Paired Device A - iPhone, ingress, we are A2DP Sink / AVRCP CT / HFP HF. */
    val iphoneAddress: String,
    /** Paired Device B - car stereo, egress, we are A2DP Source. */
    val carAddress: String
)

/**
 * One switchable audio routing engine. Implementations must be idempotent:
 * calling [start] twice re-applies the configuration, [stop] on an idle engine
 * is a no-op.
 */
interface RelayEngine {
    val mode: RelayMode

    /** Bring the audio path up for the given endpoints. */
    suspend fun start(endpoints: RelayEndpoints): EngineStatus

    /** Tear the audio path down. Must never throw. */
    suspend fun stop()

    /** Current counters (cheap, callable from a UI ticker). */
    fun stats(): EngineStats

    /** Current status without side effects. */
    fun status(): EngineStatus
}

/** Bluetooth identifiers that are hidden or @SystemApi on SDK 30. */
object BtConstants {
    const val BLUETOOTH_PACKAGE = "com.android.bluetooth"
    const val MEDIA_BROWSER_SERVICE = "com.android.bluetooth.avrcpcontroller.BluetoothMediaBrowserService"

    // BluetoothProfile ids (frameworks/base/core/java/android/bluetooth/BluetoothProfile.java, R)
    const val PROFILE_HEADSET = 1
    const val PROFILE_A2DP = 2
    const val PROFILE_A2DP_SINK = 11
    const val PROFILE_AVRCP_CONTROLLER = 12
    const val PROFILE_HEADSET_CLIENT = 16
    const val PROFILE_PBAP_CLIENT = 17

    // BluetoothProfile connection policies (SDK 30 contract)
    const val CONNECTION_POLICY_UNKNOWN = -1
    const val CONNECTION_POLICY_FORBIDDEN = 0
    const val CONNECTION_POLICY_ALLOWED = 100

    // Legacy priority values kept as a fallback for setPriority()
    const val PRIORITY_OFF = 0
    const val PRIORITY_ON = 100

    // Connection states
    const val STATE_DISCONNECTED = 0
    const val STATE_CONNECTING = 1
    const val STATE_CONNECTED = 2
    const val STATE_DISCONNECTING = 3

    // Broadcast actions (hidden on SDK 30 except the A2DP source one)
    const val ACTION_A2DP_CONNECTION_STATE_CHANGED =
        "android.bluetooth.a2dp.profile.action.CONNECTION_STATE_CHANGED"
    const val ACTION_A2DP_ACTIVE_DEVICE_CHANGED =
        "android.bluetooth.a2dp.profile.action.ACTIVE_DEVICE_CHANGED"
    const val ACTION_A2DP_CODEC_CONFIG_CHANGED =
        "android.bluetooth.a2dp.profile.action.CODEC_CONFIG_CHANGED"
    const val ACTION_A2DP_SINK_CONNECTION_STATE_CHANGED =
        "android.bluetooth.a2dp-sink.profile.action.CONNECTION_STATE_CHANGED"
    const val ACTION_A2DP_SINK_PLAYING_STATE_CHANGED =
        "android.bluetooth.a2dp-sink.profile.action.PLAYING_STATE_CHANGED"
    const val ACTION_AVRCP_CONTROLLER_CONNECTION_STATE_CHANGED =
        "android.bluetooth.avrcp-controller.profile.action.CONNECTION_STATE_CHANGED"
    const val ACTION_HEADSET_CLIENT_CONNECTION_STATE_CHANGED =
        "android.bluetooth.headsetclient.profile.action.CONNECTION_STATE_CHANGED"
    const val ACTION_HEADSET_CONNECTION_STATE_CHANGED =
        "android.bluetooth.headset.profile.action.CONNECTION_STATE_CHANGED"
    const val EXTRA_CODEC_STATUS = "android.bluetooth.extra.CODEC_STATUS"

    // BluetoothCodecConfig sample-rate bit-field (R)
    const val SAMPLE_RATE_44100 = 0x01
    const val SAMPLE_RATE_48000 = 0x02
    const val SAMPLE_RATE_88200 = 0x04
    const val SAMPLE_RATE_96000 = 0x08

    fun codecSampleRateToHz(mask: Int): Int = when {
        mask and SAMPLE_RATE_96000 != 0 -> 96000
        mask and SAMPLE_RATE_88200 != 0 -> 88200
        mask and SAMPLE_RATE_48000 != 0 -> 48000
        mask and SAMPLE_RATE_44100 != 0 -> 44100
        else -> 0
    }

    fun codecTypeName(type: Int): String = when (type) {
        0 -> "SBC"
        1 -> "AAC"
        2 -> "aptX"
        3 -> "aptX HD"
        4 -> "LDAC"
        else -> "codec#$type"
    }

    fun stateName(state: Int): String = when (state) {
        STATE_DISCONNECTED -> "disconnected"
        STATE_CONNECTING -> "connecting"
        STATE_CONNECTED -> "connected"
        STATE_DISCONNECTING -> "disconnecting"
        else -> "state#$state"
    }
}
