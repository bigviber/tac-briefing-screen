package com.local.carhome

import android.annotation.SuppressLint
import android.bluetooth.BluetoothA2dp
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothClass
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothProfile
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import androidx.compose.runtime.Stable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue

/** Which side of the audio route a device is on: phones stream TO the tablet, cars receive FROM it. */
enum class Side { PHONE, CAR }

data class BtDev(val address: String, val name: String, val side: Side, val bonded: Boolean, val connected: Boolean, val streaming: Boolean, val pairing: Boolean)

/**
 * The tablet's two Bluetooth audio roles, managed from Car Home:
 * - PHONE side: the tablet is an A2DP sink (our LSPosed hook keeps this running next to the source).
 * - CAR side: the tablet is an A2DP source. Whatever plays on the tablet, including the phone's stream (played by the
 *   Bluetooth stack as a normal Android audio track), goes to the car once the car is the active output.
 * Connect / disconnect / visibility / pairing confirmation are system calls that need BLUETOOTH_PRIVILEGED; Car Home has
 * it as a privileged app (Magisk module carhome_priv). Without it, pairing still works through Android's own dialogs.
 */
@Stable
class BtLink(private val ctx: Context) {
    private val main = Handler(Looper.getMainLooper())
    private val adapter: BluetoothAdapter? = BluetoothAdapter.getDefaultAdapter()
    private val prefs = ctx.getSharedPreferences("bt_sides", Context.MODE_PRIVATE)

    var on by mutableStateOf(false); private set
    var phones by mutableStateOf<List<BtDev>>(emptyList()); private set
    var cars by mutableStateOf<List<BtDev>>(emptyList()); private set
    /** Nearby, not yet paired devices found while the car scan runs. */
    var found by mutableStateOf<List<BtDev>>(emptyList()); private set
    var scanning by mutableStateOf(false); private set
    /** elapsedRealtime until which the tablet is visible to phones (0 = not visible). */
    var visibleUntil by mutableLongStateOf(0L); private set
    var message by mutableStateOf<String?>(null); private set

    private var a2dp: BluetoothA2dp? = null
    private var sink: BluetoothProfile? = null
    private var hfc: BluetoothProfile? = null       // hands-free client: the phone's calls (see CallLink)
    private val nearby = LinkedHashMap<String, BluetoothDevice>()
    private var pairingPhone = false               // the tablet was made visible from the Bluetooth screen
    private var pairingCar = false                 // a car scan was started from the Bluetooth screen
    private var autoTried = false                  // the paired phone is asked to connect once per start

    // ------------------------------------------------------------------ state

    @SuppressLint("MissingPermission")
    private fun sideOf(d: BluetoothDevice): Side {
        prefs.getString(d.address, null)?.let { return Side.valueOf(it) }
        if (sink?.getConnectionState(d) == BluetoothProfile.STATE_CONNECTED || hfc?.getConnectionState(d) == BluetoothProfile.STATE_CONNECTED) return Side.PHONE
        val major = d.bluetoothClass?.majorDeviceClass
        return if (major == BluetoothClass.Device.Major.PHONE || major == BluetoothClass.Device.Major.COMPUTER) Side.PHONE else Side.CAR
    }

    @SuppressLint("MissingPermission")
    private fun dev(d: BluetoothDevice, side: Side = sideOf(d)): BtDev {
        val connected = when (side) {
            Side.PHONE -> sink?.getConnectionState(d) == BluetoothProfile.STATE_CONNECTED || hfc?.getConnectionState(d) == BluetoothProfile.STATE_CONNECTED
            Side.CAR -> a2dp?.getConnectionState(d) == BluetoothProfile.STATE_CONNECTED
        }
        val streaming = connected && when (side) {
            Side.PHONE -> call(sink, "isA2dpPlaying", d) as? Boolean ?: false
            Side.CAR -> a2dp?.isA2dpPlaying(d) == true
        }
        return BtDev(d.address, d.name ?: d.address, side, d.bondState == BluetoothDevice.BOND_BONDED, connected, streaming,
            d.bondState == BluetoothDevice.BOND_BONDING)
    }

    @SuppressLint("MissingPermission")
    fun refresh() {
        val a = adapter ?: return
        on = a.isEnabled
        val bonded = runCatching { a.bondedDevices.toList() }.getOrDefault(emptyList()).map { dev(it) }
        phones = bonded.filter { it.side == Side.PHONE }.sortedByDescending { it.connected }
        cars = bonded.filter { it.side == Side.CAR }.sortedByDescending { it.connected }
        found = nearby.values.filter { it.bondState != BluetoothDevice.BOND_BONDED }.map { dev(it, Side.CAR) }
            .sortedBy { if (it.name.contains(':')) 1 else 0 }
        if (visibleUntil != 0L && SystemClock.elapsedRealtime() > visibleUntil) visibleUntil = 0L
        routeToCar()
        reconnectPhone()
    }

    /**
     * After a reboot nothing reconnects a phone to a tablet in these roles (Android only does it for headsets and cars,
     * and an iPhone may not try): once the profiles are up, the tablet asks the paired phone for calls and music itself.
     */
    private fun reconnectPhone() {
        if (autoTried || !on || sink == null || hfc == null) return
        autoTried = true
        phones.firstOrNull()?.takeIf { !it.connected }?.let { p -> main.postDelayed({ connect(p.address, Side.PHONE) }, 2000) }
    }

    /** The car is the audio output whenever it is connected, so phone audio + tablet audio both go to it. */
    private fun routeToCar() {
        val car = cars.firstOrNull { it.connected } ?: return
        val d = adapter?.getRemoteDevice(car.address) ?: return
        val active = call(a2dp, "getActiveDevice") as? BluetoothDevice
        if (active?.address != car.address) call(a2dp, "setActiveDevice", d)
    }

    // ------------------------------------------------------------------ actions

    /** Pair phone: make the tablet visible for [seconds] and auto-confirm the phone's pairing request. */
    @SuppressLint("MissingPermission")
    fun startPhonePairing(seconds: Int = 180) {
        pairingPhone = true
        val ok = call(adapter, "setScanMode", BluetoothAdapter.SCAN_MODE_CONNECTABLE_DISCOVERABLE, seconds) as? Boolean ?: false
        if (ok) visibleUntil = SystemClock.elapsedRealtime() + seconds * 1000L
        else runCatching {   // without the privileged permission Android asks for consent itself
            ctx.startActivity(Intent(BluetoothAdapter.ACTION_REQUEST_DISCOVERABLE)
                .putExtra(BluetoothAdapter.EXTRA_DISCOVERABLE_DURATION, seconds).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            visibleUntil = SystemClock.elapsedRealtime() + seconds * 1000L
        }
        refresh()
    }

    /** Pair car: scan for nearby devices. */
    @SuppressLint("MissingPermission")
    fun startCarScan() {
        pairingCar = true
        nearby.clear()
        adapter?.cancelDiscovery()
        scanning = adapter?.startDiscovery() == true
        if (!scanning) message = "Could not start scanning (is Location on?)"
        refresh()
    }

    @SuppressLint("MissingPermission")
    fun stopPairing() {
        pairingPhone = false; pairingCar = false
        adapter?.cancelDiscovery()
        if (visibleUntil != 0L) call(adapter, "setScanMode", BluetoothAdapter.SCAN_MODE_CONNECTABLE, 0)
        visibleUntil = 0L; scanning = false; message = null
        refresh()
    }

    @SuppressLint("MissingPermission")
    fun pair(address: String, side: Side) {
        val d = adapter?.getRemoteDevice(address) ?: return
        prefs.edit().putString(address, side.name).apply()
        adapter.cancelDiscovery()
        if (!d.createBond()) message = "Pairing could not start"
        refresh()
    }

    fun connect(address: String, side: Side) {
        val d = adapter?.getRemoteDevice(address) ?: return
        prefs.edit().putString(address, side.name).apply()
        val profiles = if (side == Side.PHONE) listOf(sink, hfc) else listOf(a2dp)
        var ok = false
        for (p in profiles) {
            call(p, "setConnectionPolicy", d, 100)      // CONNECTION_POLICY_ALLOWED: reconnect automatically
            if (p?.getConnectionState(d) != BluetoothProfile.STATE_CONNECTED) ok = (call(p, "connect", d) as? Boolean ?: false) || ok else ok = true
        }
        if (!ok) message = "Connect needs the privileged Car Home (or connect from the other device)"
        main.postDelayed({ refresh() }, 1500)
    }

    fun disconnect(address: String, side: Side) {
        val d = adapter?.getRemoteDevice(address) ?: return
        for (p in if (side == Side.PHONE) listOf(hfc, sink) else listOf(a2dp)) call(p, "disconnect", d)
        main.postDelayed({ refresh() }, 1000)
    }

    fun forget(address: String) {
        val d = adapter?.getRemoteDevice(address) ?: return
        call(d, "removeBond")
        prefs.edit().remove(address).apply()
        main.postDelayed({ refresh() }, 1000)
    }

    // ------------------------------------------------------------------ events

    private val rx = object : BroadcastReceiver() {
        @SuppressLint("MissingPermission")
        override fun onReceive(c: Context, i: Intent) {
            val d: BluetoothDevice? = i.getParcelableExtra(BluetoothDevice.EXTRA_DEVICE)
            when (i.action) {
                BluetoothDevice.ACTION_FOUND -> if (d != null && pairingCar) nearby[d.address] = d
                BluetoothAdapter.ACTION_STATE_CHANGED -> if (i.getIntExtra(BluetoothAdapter.EXTRA_STATE, 0) == BluetoothAdapter.STATE_ON) autoTried = false
                // An iPhone connects calls to a tablet by itself but sends music only when asked: ask as soon as calls connect.
                CallLink.HF_CONNECTION -> if (d != null && i.getIntExtra(BluetoothProfile.EXTRA_STATE, 0) == BluetoothProfile.STATE_CONNECTED &&
                    sink?.getConnectionState(d) == BluetoothProfile.STATE_DISCONNECTED) main.postDelayed({ connect(d.address, Side.PHONE) }, 1500)
                BluetoothAdapter.ACTION_DISCOVERY_FINISHED -> scanning = false
                BluetoothDevice.ACTION_PAIRING_REQUEST -> if (d != null && (pairingPhone || pairingCar)) {
                    // We started this pairing from Car Home: confirm on the tablet side so only the other device needs a tap.
                    call(d, "setPairingConfirmation", true)
                }
                BluetoothDevice.ACTION_BOND_STATE_CHANGED -> if (d != null && i.getIntExtra(BluetoothDevice.EXTRA_BOND_STATE, 0) == BluetoothDevice.BOND_BONDED) {
                    // Both kinds of pairing can run at once: pair() has already saved the side of a car picked here, a device that
                    // comes to us while only one kind runs is that kind, and otherwise its Bluetooth class decides.
                    val side = prefs.getString(d.address, null)?.let { Side.valueOf(it) }
                        ?: if (pairingPhone && !pairingCar) Side.PHONE else if (pairingCar && !pairingPhone) Side.CAR else sideOf(d)
                    prefs.edit().putString(d.address, side.name).apply()
                    if (side == Side.CAR) main.postDelayed({ connect(d.address, Side.CAR) }, 1200)   // phones connect to us themselves
                }
            }
            refresh()
        }
    }

    private val proxies = object : BluetoothProfile.ServiceListener {
        override fun onServiceConnected(profile: Int, proxy: BluetoothProfile) {
            when (profile) { BluetoothProfile.A2DP -> a2dp = proxy as BluetoothA2dp; A2DP_SINK -> sink = proxy; else -> hfc = proxy }
            refresh()
        }
        override fun onServiceDisconnected(profile: Int) {
            when (profile) { BluetoothProfile.A2DP -> a2dp = null; A2DP_SINK -> sink = null; else -> hfc = null }
            refresh()
        }
    }

    fun start() {
        val a = adapter ?: return
        ctx.registerReceiver(rx, IntentFilter().apply {
            listOf(BluetoothDevice.ACTION_FOUND, BluetoothAdapter.ACTION_DISCOVERY_FINISHED, BluetoothAdapter.ACTION_STATE_CHANGED,
                BluetoothAdapter.ACTION_SCAN_MODE_CHANGED, BluetoothDevice.ACTION_BOND_STATE_CHANGED, BluetoothDevice.ACTION_ACL_CONNECTED,
                BluetoothDevice.ACTION_ACL_DISCONNECTED, BluetoothA2dp.ACTION_CONNECTION_STATE_CHANGED, BluetoothA2dp.ACTION_PLAYING_STATE_CHANGED,
                SINK_CONNECTION, SINK_PLAYING, CallLink.HF_CONNECTION).forEach { addAction(it) }
            addAction(BluetoothDevice.ACTION_PAIRING_REQUEST)
            priority = IntentFilter.SYSTEM_HIGH_PRIORITY - 1
        })
        a.getProfileProxy(ctx, proxies, BluetoothProfile.A2DP)
        a.getProfileProxy(ctx, proxies, A2DP_SINK)
        a.getProfileProxy(ctx, proxies, CallLink.HEADSET_CLIENT)
        refresh()
    }

    @SuppressLint("MissingPermission")
    fun stop() {
        runCatching { ctx.unregisterReceiver(rx) }
        if (pairingPhone || pairingCar) stopPairing()
        autoTried = false
        a2dp?.let { adapter?.closeProfileProxy(BluetoothProfile.A2DP, it) }; a2dp = null
        sink?.let { adapter?.closeProfileProxy(A2DP_SINK, it) }; sink = null
        hfc?.let { adapter?.closeProfileProxy(CallLink.HEADSET_CLIENT, it) }; hfc = null
    }

    companion object {
        const val A2DP_SINK = 11   // BluetoothProfile.A2DP_SINK (system API)
        const val SINK_CONNECTION = "android.bluetooth.a2dp-sink.profile.action.CONNECTION_STATE_CHANGED"
        const val SINK_PLAYING = "android.bluetooth.a2dp-sink.profile.action.PLAYING_STATE_CHANGED"

        /** Calls a hidden / system method by name (exempted from hidden-API checks via the global setting, see reapply.sh). */
        fun call(target: Any?, name: String, vararg args: Any): Any? {
            if (target == null) return null
            val m = target.javaClass.methods.firstOrNull { m ->
                m.name == name && m.parameterTypes.size == args.size && m.parameterTypes.zip(args).all { (t, a) ->
                    t.isInstance(a) || (t == Int::class.javaPrimitiveType && a is Int) || (t == Long::class.javaPrimitiveType && a is Int) ||
                        (t == Boolean::class.javaPrimitiveType && a is Boolean)
                }
            } ?: return null
            val fixed = m.parameterTypes.zip(args).map { (t, a) -> if (t == Long::class.javaPrimitiveType && a is Int) a.toLong() else a }
            return runCatching { m.invoke(target, *fixed.toTypedArray()) }.getOrNull()
        }
    }
}
