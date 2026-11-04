package com.carivi.relay

import android.content.Context
import android.util.Log
import com.carivi.relay.audio.AudioDeviceResolver
import com.carivi.relay.audiopatch.AudioPatchBridge
import com.carivi.relay.bt.BtProfileProxies
import com.carivi.relay.bt.BtRoleCoordinator
import com.carivi.relay.bt.RelayPrefs
import com.carivi.relay.core.EngineStats
import com.carivi.relay.core.EngineStatus
import com.carivi.relay.core.RelayEndpoints
import com.carivi.relay.core.RelayEngine
import com.carivi.relay.core.RelayMode
import com.carivi.relay.engine.FocusRoutingEngine
import com.carivi.relay.engine.OboeBridgeEngine
import com.carivi.relay.media.NowPlayingController
import com.carivi.relay.root.RootShell
import com.carivi.relay.root.SystemBootstrap
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Process-wide orchestrator: owns the Bluetooth coordinator, the three relay
 * engines, the Now Playing controller and the dashboard state.
 */
class RelayController(private val context: Context) {
    private val tag = "CarRelay.Controller"
    val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)

    val prefs = RelayPrefs(context)
    private val proxies = BtProfileProxies(context)
    val coordinator = BtRoleCoordinator(context, proxies, prefs, scope)
    val nowPlaying = NowPlayingController(context)
    private val resolver = AudioDeviceResolver(context)
    private val bootstrap = SystemBootstrap(context)

    private val audioPatchBridge = AudioPatchBridge(context)
    private val engines: Map<RelayMode, RelayEngine> = mapOf(
        RelayMode.NATIVE_FOCUS to FocusRoutingEngine(context, coordinator, nowPlaying, resolver),
        RelayMode.OBOE_BRIDGE to OboeBridgeEngine(context, resolver, prefs),
        RelayMode.AUDIO_PATCH to audioPatchBridge
    )
    private val engineLock = Mutex()
    private var activeEngine: RelayEngine? = null

    data class BtDeviceUi(val name: String, val address: String)

    data class DashboardState(
        val mode: RelayMode = RelayMode.NATIVE_FOCUS,
        val engineStatus: EngineStatus = EngineStatus.Idle,
        val relayArmed: Boolean = false,
        val stats: EngineStats = EngineStats(),
        val bonded: List<BtDeviceUi> = emptyList(),
        val iphoneAddress: String? = null,
        val carAddress: String? = null,
        val profiles: BtRoleCoordinator.ProfileSnapshot = BtRoleCoordinator.ProfileSnapshot(),
        val nowPlaying: NowPlayingController.NowPlaying = NowPlayingController.NowPlaying(),
        val patchStrategy: AudioPatchBridge.Strategy = AudioPatchBridge.Strategy.DEVICE_PATCH,
        val forceLoopback: Boolean = false,
        val autoSequenceOnPower: Boolean = true,
        val hfpClientEnabled: Boolean = true,
        val carHfpAgEnabled: Boolean = false,
        val rootAvailable: Boolean = false,
        val privileged: Boolean = false,
        val sequencerBusy: Boolean = false,
        val audioDevices: List<String> = emptyList(),
        val log: List<String> = emptyList()
    )

    private data class Internal(
        val mode: RelayMode,
        val engineStatus: EngineStatus = EngineStatus.Idle,
        val relayArmed: Boolean = false,
        val stats: EngineStats = EngineStats(),
        val bonded: List<BtDeviceUi> = emptyList(),
        val iphoneAddress: String?,
        val carAddress: String?,
        val patchStrategy: AudioPatchBridge.Strategy,
        val forceLoopback: Boolean,
        val autoSequenceOnPower: Boolean,
        val hfpClientEnabled: Boolean,
        val carHfpAgEnabled: Boolean,
        val rootAvailable: Boolean = false,
        val privileged: Boolean = false,
        val sequencerBusy: Boolean = false,
        val audioDevices: List<String> = emptyList(),
        val log: List<String> = emptyList()
    )

    private val internal = MutableStateFlow(
        Internal(
            mode = prefs.mode,
            iphoneAddress = prefs.iphoneAddress,
            carAddress = prefs.carAddress,
            patchStrategy = prefs.patchStrategy,
            forceLoopback = prefs.forceLoopback,
            autoSequenceOnPower = prefs.autoSequenceOnPower,
            hfpClientEnabled = prefs.hfpClientEnabled,
            carHfpAgEnabled = prefs.carHfpAgEnabled
        )
    )

    val state: StateFlow<DashboardState> = combine(
        internal, coordinator.snapshot, nowPlaying.state
    ) { i, snap, np ->
        DashboardState(
            mode = i.mode, engineStatus = i.engineStatus, relayArmed = i.relayArmed, stats = i.stats,
            bonded = i.bonded, iphoneAddress = i.iphoneAddress, carAddress = i.carAddress,
            profiles = snap, nowPlaying = np, patchStrategy = i.patchStrategy,
            forceLoopback = i.forceLoopback, autoSequenceOnPower = i.autoSequenceOnPower,
            hfpClientEnabled = i.hfpClientEnabled, carHfpAgEnabled = i.carHfpAgEnabled,
            rootAvailable = i.rootAvailable, privileged = i.privileged,
            sequencerBusy = i.sequencerBusy || coordinator.sequencerBusy,
            audioDevices = i.audioDevices, log = i.log
        )
    }.stateIn(scope, SharingStarted.Eagerly, DashboardState())

    private var started = false
    private val timeFmt = SimpleDateFormat("HH:mm:ss", Locale.US)

    // ---------------------------------------------------------------------
    // Lifecycle
    // ---------------------------------------------------------------------
    fun start() {
        if (started) return
        started = true
        audioPatchBridge.strategy = prefs.patchStrategy
        coordinator.start()
        nowPlaying.connect()
        scope.launch { coordinator.events.collect { log(it) } }
        resolver.registerCallback { refreshAudioDevices() }
        refreshBonded()
        refreshAudioDevices()
        scope.launch {
            val root = RootShell.isRootAvailable()
            val priv = bootstrap.isPrivileged()
            internal.update { it.copy(rootAvailable = root, privileged = priv) }
            log("controller up · root=$root · privileged=$priv · mode=${prefs.mode.title}")
            if (!priv) log("! BLUETOOTH_PRIVILEGED/MODIFY_AUDIO_ROUTING not granted - install via the Magisk module (priv-app + whitelist)")
        }
    }

    fun shutdown() {
        scope.launch { stopEngineLocked() }
        coordinator.stop()
        nowPlaying.disconnect()
        proxies.close()
        scope.cancel()
    }

    fun endpointsOrNull(): RelayEndpoints? {
        val i = prefs.iphoneAddress ?: return null
        val c = prefs.carAddress ?: return null
        if (i.isBlank() || c.isBlank() || i.equals(c, ignoreCase = true)) return null
        return RelayEndpoints(i, c)
    }

    // ---------------------------------------------------------------------
    // Dashboard actions
    // ---------------------------------------------------------------------
    fun selectIphone(address: String?) {
        prefs.iphoneAddress = address
        internal.update { it.copy(iphoneAddress = address) }
        log("iPhone [Ingress Sink] = ${address ?: "none"}")
        coordinator.refresh()
    }

    fun selectCar(address: String?) {
        prefs.carAddress = address
        internal.update { it.copy(carAddress = address) }
        log("Car [Egress Source] = ${address ?: "none"}")
        coordinator.refresh()
    }

    fun selectMode(mode: RelayMode) {
        if (mode == prefs.mode && activeEngine?.mode == mode) return
        prefs.mode = mode
        internal.update { it.copy(mode = mode) }
        log("architecture → ${mode.title}")
        scope.launch {
            val wasArmed = internal.value.relayArmed
            stopEngineLocked()
            if (wasArmed) startEngineLocked()
        }
    }

    fun setPatchStrategy(strategy: AudioPatchBridge.Strategy) {
        prefs.patchStrategy = strategy
        audioPatchBridge.strategy = strategy
        internal.update { it.copy(patchStrategy = strategy) }
        log("Mode 3 strategy → $strategy")
        if (internal.value.relayArmed && prefs.mode == RelayMode.AUDIO_PATCH) scope.launch { startEngineLocked() }
    }

    fun setForceLoopback(v: Boolean) {
        prefs.forceLoopback = v
        internal.update { it.copy(forceLoopback = v) }
        log("Mode 2 ingress → ${if (v) "uid loop-back (forced)" else "HAL a2dp input, loop-back fallback"}")
        if (internal.value.relayArmed && prefs.mode == RelayMode.OBOE_BRIDGE) scope.launch { startEngineLocked() }
    }

    fun setAutoSequence(v: Boolean) { prefs.autoSequenceOnPower = v; internal.update { it.copy(autoSequenceOnPower = v) } }
    fun setHfpClient(v: Boolean) { prefs.hfpClientEnabled = v; internal.update { it.copy(hfpClientEnabled = v) } }
    fun setCarHfpAg(v: Boolean) { prefs.carHfpAgEnabled = v; internal.update { it.copy(carHfpAgEnabled = v) } }

    fun startRelay() {
        internal.update { it.copy(relayArmed = true) }
        scope.launch { startEngineLocked() }
    }

    fun stopRelay() {
        internal.update { it.copy(relayArmed = false) }
        scope.launch { stopEngineLocked() }
    }

    fun runSequence(thenStart: Boolean = true) {
        val ep = endpointsOrNull() ?: run { log("! pick both devices first"); return }
        scope.launch {
            internal.update { it.copy(sequencerBusy = true) }
            val ok = coordinator.connectSequence(ep)
            internal.update { it.copy(sequencerBusy = false) }
            if (ok && thenStart) {
                internal.update { it.copy(relayArmed = true) }
                startEngineLocked()
            }
        }
    }

    fun pinRoles() {
        val ep = endpointsOrNull() ?: run { log("! pick both devices first"); return }
        scope.launch { coordinator.pinRoles(ep) }
    }

    fun disconnectAll() {
        val ep = endpointsOrNull() ?: return
        scope.launch {
            stopEngineLocked()
            internal.update { it.copy(relayArmed = false) }
            coordinator.disconnectAll(ep)
        }
    }

    fun applyBootstrap() {
        scope.launch {
            log("applying Solution 1 runtime activation…")
            bootstrap.apply().forEach { log(it) }
            internal.update { it.copy(privileged = bootstrap.isPrivileged(), rootAvailable = RootShell.isRootAvailable()) }
        }
    }

    fun refreshDiagnostics() {
        scope.launch {
            refreshBonded()
            refreshAudioDevices()
            coordinator.refresh()
            bootstrap.diagnostics().forEach { log(it) }
        }
    }

    /** Called once a second by the ViewModel ticker. */
    fun tick() {
        val engine = activeEngine
        internal.update {
            it.copy(
                stats = engine?.stats() ?: EngineStats(),
                engineStatus = engine?.status() ?: if (it.relayArmed) it.engineStatus else EngineStatus.Idle
            )
        }
    }

    fun onPowerConnected() {
        log("car power detected (ACTION_POWER_CONNECTED)")
        if (prefs.autoSequenceOnPower && endpointsOrNull() != null) runSequence(thenStart = true)
    }

    fun onPowerDisconnected() {
        log("car power lost (ACTION_POWER_DISCONNECTED) - parking relay")
        stopRelay()
    }

    fun onAdapterOn() {
        refreshBonded()
        coordinator.refresh()
        nowPlaying.connect()
    }

    // ---------------------------------------------------------------------
    // Engine plumbing
    // ---------------------------------------------------------------------
    private suspend fun startEngineLocked() = engineLock.withLock {
        val ep = endpointsOrNull()
        if (ep == null) {
            internal.update { it.copy(engineStatus = EngineStatus.Failed("pick both devices first")) }
            return@withLock
        }
        val target = engines.getValue(prefs.mode)
        activeEngine?.let { if (it !== target) it.stop() }
        activeEngine = target
        internal.update { it.copy(engineStatus = EngineStatus.Starting(target.mode.title)) }
        val result = try {
            target.start(ep)
        } catch (t: Throwable) {
            Log.e(tag, "engine start crashed", t)
            EngineStatus.Failed("${t.javaClass.simpleName}: ${t.message}")
        }
        internal.update { it.copy(engineStatus = result, stats = target.stats()) }
        log("${target.mode.title}: ${result.describe()}")
    }

    private suspend fun stopEngineLocked() = engineLock.withLock {
        activeEngine?.let {
            try { it.stop() } catch (t: Throwable) { Log.w(tag, "engine stop failed", t) }
            log("${it.mode.title}: stopped")
        }
        activeEngine = null
        internal.update { it.copy(engineStatus = EngineStatus.Idle, stats = EngineStats()) }
    }

    private fun refreshBonded() {
        val list = coordinator.bondedDevices().map {
            BtDeviceUi(name = it.name ?: it.address, address = it.address)
        }.sortedBy { it.name.lowercase() }
        internal.update { it.copy(bonded = list) }
    }

    private fun refreshAudioDevices() {
        internal.update { it.copy(audioDevices = resolver.describe()) }
    }

    fun log(line: String) {
        Log.i(tag, line)
        val stamped = "${timeFmt.format(Date())}  $line"
        internal.update { it.copy(log = (it.log + stamped).takeLast(MAX_LOG)) }
    }

    companion object {
        private const val MAX_LOG = 300
    }
}
