package com.carivi.relay.bt

import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothProfile
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.Parcelable
import android.util.Log
import com.carivi.relay.core.BtConstants
import com.carivi.relay.core.BtConstants.CONNECTION_POLICY_ALLOWED
import com.carivi.relay.core.BtConstants.CONNECTION_POLICY_FORBIDDEN
import com.carivi.relay.core.BtConstants.PROFILE_A2DP
import com.carivi.relay.core.BtConstants.PROFILE_A2DP_SINK
import com.carivi.relay.core.BtConstants.PROFILE_AVRCP_CONTROLLER
import com.carivi.relay.core.BtConstants.PROFILE_HEADSET
import com.carivi.relay.core.BtConstants.PROFILE_HEADSET_CLIENT
import com.carivi.relay.core.BtConstants.STATE_CONNECTED
import com.carivi.relay.core.BtConstants.STATE_DISCONNECTED
import com.carivi.relay.core.RelayEndpoints
import com.carivi.relay.core.reflect.Hidden
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withTimeoutOrNull

/**
 * SDK 30 dual-role connection coordinator.
 *
 * Strict role pinning (per paired device, via setConnectionPolicy):
 *
 *   iPhone (ingress)        car stereo (egress)
 *   A2DP source  FORBIDDEN  A2DP source  ALLOWED
 *   A2DP sink    ALLOWED    A2DP sink    FORBIDDEN
 *   HFP client   ALLOWED*   HFP client   FORBIDDEN
 *   HFP AG       FORBIDDEN  HFP AG       pref (default FORBIDDEN)
 *   AVRCP CT     (no policy API on SDK 30 - follows the A2DP sink link)
 *
 * Connection sequencer: car A2DP source first, wait for CONNECTED, hold
 * 2500 ms for ACL/codec negotiation to settle, then the iPhone A2DP sink
 * (+ HFP client), then pin the car as the active A2DP device.
 */
class BtRoleCoordinator(
    private val context: Context,
    private val proxies: BtProfileProxies,
    private val prefs: RelayPrefs,
    private val scope: CoroutineScope
) {
    private val tag = "CarRelay.Roles"

    data class ProfileSnapshot(
        val a2dpSourceCar: Int = STATE_DISCONNECTED,
        val a2dpSinkIphone: Int = STATE_DISCONNECTED,
        val avrcpControllerIphone: Int = STATE_DISCONNECTED,
        val hfpClientIphone: Int = STATE_DISCONNECTED,
        val hfpAgCar: Int = STATE_DISCONNECTED,
        val activeA2dpAddress: String? = null,
        val carCodec: String? = null,
        val carCodecSampleRate: Int = 0,
        val sinkPlaying: Boolean = false,
        val adapterOn: Boolean = false
    )

    private val _snapshot = MutableStateFlow(ProfileSnapshot())
    val snapshot: StateFlow<ProfileSnapshot> = _snapshot.asStateFlow()

    private val _events = MutableSharedFlow<String>(extraBufferCapacity = 64)
    val events: SharedFlow<String> = _events.asSharedFlow()

    private val sequencerLock = Mutex()
    @Volatile var sequencerBusy: Boolean = false
        private set

    private var receiverRegistered = false

    private val receiver = object : BroadcastReceiver() {
        override fun onReceive(ctx: Context, intent: Intent) {
            val action = intent.action ?: return
            val device = intent.getParcelableExtra<BluetoothDevice>(BluetoothDevice.EXTRA_DEVICE)
            val state = intent.getIntExtra(BluetoothProfile.EXTRA_STATE, -1)
            when (action) {
                BtConstants.ACTION_A2DP_CODEC_CONFIG_CHANGED -> {
                    val status = intent.getParcelableExtra<Parcelable>(BtConstants.EXTRA_CODEC_STATUS)
                    val config = status?.let { Hidden.call<Any?>(it, "getCodecConfig").valueOrNull() }
                    val type = config?.let { Hidden.call<Int>(it, "getCodecType").valueOrNull() }
                    val rateMask = config?.let { Hidden.call<Int>(it, "getSampleRate").valueOrNull() } ?: 0
                    val name = type?.let { BtConstants.codecTypeName(it) }
                    _snapshot.value = _snapshot.value.copy(
                        carCodec = name,
                        carCodecSampleRate = BtConstants.codecSampleRateToHz(rateMask)
                    )
                    emit("car codec: $name @ ${BtConstants.codecSampleRateToHz(rateMask)} Hz")
                }
                BtConstants.ACTION_A2DP_ACTIVE_DEVICE_CHANGED -> {
                    _snapshot.value = _snapshot.value.copy(activeA2dpAddress = device?.address)
                    emit("A2DP active device → ${device?.address ?: "none"}")
                }
                BtConstants.ACTION_A2DP_SINK_PLAYING_STATE_CHANGED -> {
                    val playing = state == 10 // BluetoothA2dpSink.STATE_PLAYING
                    _snapshot.value = _snapshot.value.copy(sinkPlaying = playing)
                    emit("iPhone A2DP sink stream ${if (playing) "PLAYING" else "stopped"}")
                }
                BluetoothAdapter.ACTION_STATE_CHANGED -> {
                    val on = intent.getIntExtra(BluetoothAdapter.EXTRA_STATE, -1) == BluetoothAdapter.STATE_ON
                    _snapshot.value = _snapshot.value.copy(adapterOn = on)
                    emit("adapter ${if (on) "ON" else "off"}")
                }
                else -> {
                    emit("${profileLabel(action)} ${device?.address} → ${BtConstants.stateName(state)}")
                }
            }
            refresh()
        }
    }

    fun start() {
        if (receiverRegistered) return
        val filter = IntentFilter().apply {
            addAction(BtConstants.ACTION_A2DP_CONNECTION_STATE_CHANGED)
            addAction(BtConstants.ACTION_A2DP_ACTIVE_DEVICE_CHANGED)
            addAction(BtConstants.ACTION_A2DP_CODEC_CONFIG_CHANGED)
            addAction(BtConstants.ACTION_A2DP_SINK_CONNECTION_STATE_CHANGED)
            addAction(BtConstants.ACTION_A2DP_SINK_PLAYING_STATE_CHANGED)
            addAction(BtConstants.ACTION_AVRCP_CONTROLLER_CONNECTION_STATE_CHANGED)
            addAction(BtConstants.ACTION_HEADSET_CLIENT_CONNECTION_STATE_CHANGED)
            addAction(BtConstants.ACTION_HEADSET_CONNECTION_STATE_CHANGED)
            addAction(BluetoothAdapter.ACTION_STATE_CHANGED)
        }
        context.registerReceiver(receiver, filter)
        receiverRegistered = true
        refresh()
    }

    fun stop() {
        if (!receiverRegistered) return
        context.unregisterReceiver(receiver)
        receiverRegistered = false
    }

    fun bondedDevices(): List<BluetoothDevice> = proxies.bondedDevices()

    /** Re-read every profile state for the pinned endpoints. */
    fun refresh() {
        scope.launch {
            val iphone = proxies.device(prefs.iphoneAddress)
            val car = proxies.device(prefs.carAddress)
            val active = proxies.activeA2dpDevice()
            _snapshot.value = _snapshot.value.copy(
                a2dpSourceCar = proxies.connectionState(PROFILE_A2DP, car),
                a2dpSinkIphone = proxies.connectionState(PROFILE_A2DP_SINK, iphone),
                avrcpControllerIphone = proxies.connectionState(PROFILE_AVRCP_CONTROLLER, iphone),
                hfpClientIphone = proxies.connectionState(PROFILE_HEADSET_CLIENT, iphone),
                hfpAgCar = proxies.connectionState(PROFILE_HEADSET, car),
                activeA2dpAddress = active?.address ?: _snapshot.value.activeA2dpAddress,
                sinkPlaying = proxies.isSinkPlaying(iphone),
                adapterOn = proxies.adapter?.isEnabled == true
            )
        }
    }

    // ---------------------------------------------------------------------
    // Strict role pinning
    // ---------------------------------------------------------------------
    suspend fun pinRoles(endpoints: RelayEndpoints): List<String> {
        val log = ArrayList<String>()
        val iphone = proxies.device(endpoints.iphoneAddress)
        val car = proxies.device(endpoints.carAddress)
        if (iphone == null || car == null) {
            log += "pinRoles: endpoints not resolvable (iphone=${endpoints.iphoneAddress}, car=${endpoints.carAddress})"
            return log
        }
        val hfpClient = if (prefs.hfpClientEnabled) CONNECTION_POLICY_ALLOWED else CONNECTION_POLICY_FORBIDDEN
        val carAg = if (prefs.carHfpAgEnabled) CONNECTION_POLICY_ALLOWED else CONNECTION_POLICY_FORBIDDEN

        log += "— iPhone ${iphone.address} [Ingress Sink] —"
        log += proxies.setConnectionPolicy(PROFILE_A2DP, iphone, CONNECTION_POLICY_FORBIDDEN)
        log += proxies.setConnectionPolicy(PROFILE_A2DP_SINK, iphone, CONNECTION_POLICY_ALLOWED)
        log += proxies.setConnectionPolicy(PROFILE_AVRCP_CONTROLLER, iphone, CONNECTION_POLICY_ALLOWED)
        log += proxies.setConnectionPolicy(PROFILE_HEADSET_CLIENT, iphone, hfpClient)
        log += proxies.setConnectionPolicy(PROFILE_HEADSET, iphone, CONNECTION_POLICY_FORBIDDEN)

        log += "— Car ${car.address} [Egress Source] —"
        log += proxies.setConnectionPolicy(PROFILE_A2DP, car, CONNECTION_POLICY_ALLOWED)
        log += proxies.setConnectionPolicy(PROFILE_A2DP_SINK, car, CONNECTION_POLICY_FORBIDDEN)
        log += proxies.setConnectionPolicy(PROFILE_AVRCP_CONTROLLER, car, CONNECTION_POLICY_FORBIDDEN)
        log += proxies.setConnectionPolicy(PROFILE_HEADSET_CLIENT, car, CONNECTION_POLICY_FORBIDDEN)
        log += proxies.setConnectionPolicy(PROFILE_HEADSET, car, carAg)

        log += "verify: iPhone A2DP=${proxies.getConnectionPolicy(PROFILE_A2DP, iphone)} sink=${proxies.getConnectionPolicy(PROFILE_A2DP_SINK, iphone)} | car A2DP=${proxies.getConnectionPolicy(PROFILE_A2DP, car)} sink=${proxies.getConnectionPolicy(PROFILE_A2DP_SINK, car)}"
        log.forEach { emit(it) }
        return log
    }

    // ---------------------------------------------------------------------
    // Connection sequencer
    // ---------------------------------------------------------------------
    suspend fun connectSequence(endpoints: RelayEndpoints): Boolean = sequencerLock.withLock {
        sequencerBusy = true
        try {
            val iphone = proxies.device(endpoints.iphoneAddress)
            val car = proxies.device(endpoints.carAddress)
            if (iphone == null || car == null) {
                emit("sequencer: endpoints not set")
                return false
            }
            if (proxies.adapter?.isEnabled != true) {
                emit("sequencer: adapter off, enabling")
                @Suppress("DEPRECATION")
                proxies.adapter?.enable()
                if (!awaitAdapterOn(8_000)) {
                    emit("sequencer: adapter did not come up")
                    return false
                }
            }

            pinRoles(endpoints)

            // Step 1: car first - A2DP SOURCE. The split-A2DP encoder session in
            // the ADSP is negotiated here; it must be stable before a second ACL
            // link starts competing for air time.
            emit("① connect A2DP SOURCE → car ${car.address}")
            if (proxies.connectionState(PROFILE_A2DP, car) != STATE_CONNECTED) {
                proxies.connect(PROFILE_A2DP, car)
                if (!awaitState(PROFILE_A2DP, car, STATE_CONNECTED, 20_000)) {
                    emit("✗ car A2DP source did not connect (is the stereo in pairing/connectable mode?)")
                    return false
                }
            }
            emit("✓ car A2DP source connected; settling 2500 ms (ACL/codec negotiation)")
            delay(SETTLE_MS)

            // Step 2: iPhone - A2DP SINK (+AVRCP CT implicitly) and HFP client.
            emit("② connect A2DP SINK ← iPhone ${iphone.address}")
            if (proxies.connectionState(PROFILE_A2DP_SINK, iphone) != STATE_CONNECTED) {
                proxies.connect(PROFILE_A2DP_SINK, iphone)
                if (!awaitState(PROFILE_A2DP_SINK, iphone, STATE_CONNECTED, 20_000)) {
                    emit("✗ iPhone A2DP sink did not connect (open Settings → Bluetooth on the iPhone and tap the tablet)")
                    return false
                }
            }
            emit("✓ iPhone A2DP sink connected")

            if (prefs.hfpClientEnabled && proxies.connectionState(PROFILE_HEADSET_CLIENT, iphone) != STATE_CONNECTED) {
                emit("③ connect HFP CLIENT ← iPhone")
                proxies.connect(PROFILE_HEADSET_CLIENT, iphone)
                awaitState(PROFILE_HEADSET_CLIENT, iphone, STATE_CONNECTED, 10_000)
            }

            // Step 3: make the car the active media device so APM routes the sink
            // stream (and local Maps audio) to the car link.
            emit("④ setActiveDevice(car)")
            val active = proxies.setActiveA2dpDevice(car)
            emit(if (active) "✓ car is the active A2DP device" else "! setActiveDevice(car) returned false")
            refresh()
            true
        } finally {
            sequencerBusy = false
        }
    }

    suspend fun setActiveCar(carAddress: String): Boolean {
        val car = proxies.device(carAddress) ?: return false
        val current = proxies.activeA2dpDevice()
        if (current?.address.equals(carAddress, ignoreCase = true)) return true
        val ok = proxies.setActiveA2dpDevice(car)
        emit("setActiveDevice(${car.address}) = $ok")
        refresh()
        return ok
    }

    suspend fun disconnectAll(endpoints: RelayEndpoints) {
        val iphone = proxies.device(endpoints.iphoneAddress)
        val car = proxies.device(endpoints.carAddress)
        iphone?.let {
            proxies.disconnect(PROFILE_A2DP_SINK, it)
            proxies.disconnect(PROFILE_HEADSET_CLIENT, it)
        }
        car?.let { proxies.disconnect(PROFILE_A2DP, it) }
        emit("disconnected both endpoints")
        refresh()
    }

    private suspend fun awaitState(profileId: Int, device: BluetoothDevice, wanted: Int, timeoutMs: Long): Boolean =
        withTimeoutOrNull(timeoutMs) {
            while (proxies.connectionState(profileId, device) != wanted) delay(POLL_MS)
            true
        } ?: false

    private suspend fun awaitAdapterOn(timeoutMs: Long): Boolean =
        withTimeoutOrNull(timeoutMs) {
            while (proxies.adapter?.isEnabled != true) delay(POLL_MS)
            true
        } ?: false

    private fun emit(line: String) {
        Log.i(tag, line)
        _events.tryEmit(line)
    }

    private fun profileLabel(action: String): String = when (action) {
        BtConstants.ACTION_A2DP_CONNECTION_STATE_CHANGED -> "A2DP source"
        BtConstants.ACTION_A2DP_SINK_CONNECTION_STATE_CHANGED -> "A2DP sink"
        BtConstants.ACTION_AVRCP_CONTROLLER_CONNECTION_STATE_CHANGED -> "AVRCP controller"
        BtConstants.ACTION_HEADSET_CLIENT_CONNECTION_STATE_CHANGED -> "HFP client"
        BtConstants.ACTION_HEADSET_CONNECTION_STATE_CHANGED -> "HFP AG"
        else -> action.substringAfterLast('.')
    }

    companion object {
        const val SETTLE_MS = 2_500L
        private const val POLL_MS = 250L
    }
}
