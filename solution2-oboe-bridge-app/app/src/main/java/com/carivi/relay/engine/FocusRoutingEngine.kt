package com.carivi.relay.engine

import android.content.Context
import android.media.AudioManager
import com.carivi.relay.audio.AudioDeviceResolver
import com.carivi.relay.bt.BtRoleCoordinator
import com.carivi.relay.core.BtConstants
import com.carivi.relay.core.EngineStats
import com.carivi.relay.core.EngineStatus
import com.carivi.relay.core.RelayEndpoints
import com.carivi.relay.core.RelayEngine
import com.carivi.relay.core.RelayMode
import com.carivi.relay.media.NowPlayingController
import kotlinx.coroutines.delay

/**
 * Mode 1 - native SDK 30 stack, nothing in the PCM path.
 *
 * libbluetooth_qti.so decodes the iPhone's A2DP stream and the Bluetooth
 * process plays it as a media AudioTrack (A2dpSinkStreamHandler /
 * BtifAvrcpAudioTrack, uid 1002). AudioPolicyManager routes STRATEGY_MEDIA to
 * the active A2DP device, so the only things this engine has to guarantee are:
 *
 *  1. the car is the *active* A2DP source device (BluetoothA2dp.setActiveDevice),
 *  2. the sink stream holds audio focus. With CarIvIBtOverlay installed
 *     (a2dp_sink_automatically_request_audio_focus=true) the stream handler
 *     requests focus itself on SRC_STR_START; the MediaSession prepare() call
 *     below (-> AvrcpControllerStateMachine.onPrepare -> A2dpSinkService
 *     .requestAudioFocus(device, true)) covers builds where the overlay is off.
 *
 * Local navigation prompts mix into the same car link through the normal mixer.
 */
class FocusRoutingEngine(
    private val context: Context,
    private val coordinator: BtRoleCoordinator,
    private val nowPlaying: NowPlayingController,
    private val resolver: AudioDeviceResolver
) : RelayEngine {

    private val audioManager = context.getSystemService(AudioManager::class.java)

    @Volatile private var status: EngineStatus = EngineStatus.Idle
    @Volatile private var carName: String = ""

    override val mode: RelayMode = RelayMode.NATIVE_FOCUS

    override suspend fun start(endpoints: RelayEndpoints): EngineStatus {
        status = EngineStatus.Starting("pinning active A2DP device")

        val car = resolver.carOutput(endpoints.carAddress)
        if (car == null) {
            status = EngineStatus.Failed("car A2DP output device not present - run the connection sequencer first")
            return status
        }
        carName = car.productName?.toString() ?: endpoints.carAddress

        if (!coordinator.setActiveCar(endpoints.carAddress)) {
            status = EngineStatus.Degraded("setActiveDevice(car) refused; media may still route to the speaker")
        }

        // Give AudioService time to process the A2DP active-device switch.
        delay(400)

        val snap = coordinator.snapshot.value
        if (snap.a2dpSinkIphone != BtConstants.STATE_CONNECTED) {
            status = EngineStatus.Degraded("iPhone A2DP sink not connected - routing armed, waiting for the phone")
            return status
        }

        // Device-verified (v1.0.1 run): decoding starts from the overlay's auto-focus
        // alone; the sink does NOT need an active peer. prepare()/play() through the
        // BluetoothMediaBrowserService session is only needed to make the *iPhone*
        // start playing. On this firmware that session has stayed in STATE_ERROR (7)
        // with actions=0 and never dispatched onPlay (docs/STACK_NOTES.md, Q1), so
        // report that condition instead of pretending the command was delivered.
        nowPlaying.connect()
        nowPlaying.prepare()

        val np = nowPlaying.state.value
        if (snap.avrcpControllerIphone != BtConstants.STATE_CONNECTED) {
            status = EngineStatus.Degraded("A2DP sink up but AVRCP CT not connected - press Play on the iPhone")
            return status
        }
        if (!np.connected || np.error != null) {
            status = EngineStatus.Degraded(
                "audio path armed; AVRCP browser session in error state (${np.error ?: "not connected"}) - " +
                    "remote Play unavailable, press Play on the iPhone (STACK_NOTES Q1)"
            )
            return status
        }

        if (status !is EngineStatus.Degraded) {
            status = EngineStatus.Running("sink AudioTrack → APM media → $carName")
        }
        return status
    }

    override suspend fun stop() {
        // Nothing to tear down in the audio path. We deliberately leave the
        // active device alone so a plain phone call / Maps keeps working.
        status = EngineStatus.Idle
    }

    override fun stats(): EngineStats {
        val snap = coordinator.snapshot.value
        return EngineStats(
            inputSampleRate = 0,
            outputSampleRate = snap.carCodecSampleRate,
            channelCount = 2,
            extra = buildString {
                append(snap.carCodec ?: "codec ?")
                append(" | active=").append(snap.activeA2dpAddress ?: "none")
                append(" | sink ").append(if (snap.sinkPlaying) "PLAYING" else "idle")
                append(" | music vol ").append(audioManager.getStreamVolume(AudioManager.STREAM_MUSIC))
                    .append('/').append(audioManager.getStreamMaxVolume(AudioManager.STREAM_MUSIC))
            }
        )
    }

    override fun status(): EngineStatus = status
}
