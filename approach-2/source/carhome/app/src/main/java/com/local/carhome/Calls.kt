package com.local.carhome

import android.annotation.SuppressLint
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothProfile
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.media.AudioAttributes
import android.media.AudioManager
import android.media.Ringtone
import android.media.RingtoneManager
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.os.SystemClock
import android.provider.ContactsContract
import androidx.compose.runtime.Stable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import java.util.concurrent.Executors

/** One call as the phone reports it; [since] is when it became active (elapsedRealtime), 0 until then. */
data class PhoneCall(val id: Int, val state: Int, val number: String, val name: String?, val outgoing: Boolean, val since: Long) {
    val ringing get() = state == INCOMING || state == WAITING

    companion object {   // BluetoothHeadsetClientCall states
        const val ACTIVE = 0; const val HELD = 1; const val DIALING = 2; const val ALERTING = 3; const val INCOMING = 4; const val WAITING = 5
        const val TERMINATED = 7
    }
}

/**
 * Phone calls through the tablet. The phone is paired with the tablet only, so the tablet is its hands-free unit (Bluetooth
 * HFP client, switched on by our overlay module): the phone reports its calls here, and answered calls are heard on the
 * tablet's speakers and mic (the Qualcomm audio HAL's hfp-sco path). Android's own call integration (Telecom) is off in
 * this firmware, so Car Home drives calls itself: ringing, answer, decline, end, mute, and where the audio is.
 * Caller names come from the phone's contacts, copied over PBAP once the phone allows it.
 * Runs for the whole life of the app, not only while the home screen is visible, so a call can wake the screen.
 */
@Stable
class CallLink(private val ctx: Context) {
    var calls by mutableStateOf<List<PhoneCall>>(emptyList()); private set
    /** Call audio is on the tablet (Bluetooth voice link open); false while it stays on the phone. */
    var audioOnTablet by mutableStateOf(false); private set
    var muted by mutableStateOf(false); private set
    var phoneName by mutableStateOf<String?>(null); private set

    private val main = Handler(Looper.getMainLooper())
    private val audio = ctx.getSystemService(AudioManager::class.java)
    private var hf: BluetoothProfile? = null
    private var pbap: BluetoothProfile? = null
    private val raw = HashMap<Int, Any>()                 // the stack's call objects; ending one call needs it
    private val activeSince = HashMap<Int, Long>()
    private val names = HashMap<String, String?>()        // number -> contact name, null while unknown
    private val lookups = Executors.newSingleThreadExecutor()
    private var ring: Ringtone? = null
    private var returnTo: String? = null                  // app that was in front when a call took the screen

    @SuppressLint("MissingPermission")
    private fun device(): BluetoothDevice? = runCatching { hf?.connectedDevices?.firstOrNull() }.getOrNull()

    @SuppressLint("MissingPermission")
    private fun refresh() {
        val d = device()
        phoneName = d?.name
        val list = d?.let { BtLink.call(hf, "getCurrentCalls", it) as? List<*> }.orEmpty().filterNotNull()
        raw.clear()
        val now = list.mapNotNull { c ->
            val id = BtLink.call(c, "getId") as? Int ?: return@mapNotNull null
            val state = BtLink.call(c, "getState") as? Int ?: return@mapNotNull null
            if (state == PhoneCall.TERMINATED) return@mapNotNull null
            raw[id] = c
            if (state == PhoneCall.ACTIVE && id !in activeSince) activeSince[id] = SystemClock.elapsedRealtime()
            val number = (BtLink.call(c, "getNumber") as? String).orEmpty()
            PhoneCall(id, state, number, nameFor(number), BtLink.call(c, "isOutgoing") as? Boolean ?: false, activeSince[id] ?: 0L)
        }
        activeSince.keys.retainAll(raw.keys)
        val wasRinging = calls.any { it.ringing }
        calls = now
        audioOnTablet = d != null && BtLink.call(hf, "getAudioState", d) == AUDIO_CONNECTED
        if (now.isEmpty()) {
            if (muted) setMute(false)
            names.clear()                                   // contacts may have synced since
            returnTo?.let { Apps.launch(ctx, it) }; returnTo = null
        }
        updateRing()
        if (!wasRinging && now.any { it.ringing }) takeScreen()
    }

    /** Contact name for a number, looked up off the main thread; the call row updates when it arrives. */
    private fun nameFor(number: String): String? {
        if (number.isBlank()) return null
        if (names.containsKey(number)) return names[number]
        names[number] = null
        lookups.execute {
            val n = runCatching {
                ctx.contentResolver.query(Uri.withAppendedPath(ContactsContract.PhoneLookup.CONTENT_FILTER_URI, Uri.encode(number)),
                    arrayOf(ContactsContract.PhoneLookup.DISPLAY_NAME), null, null, null)?.use { if (it.moveToFirst()) it.getString(0) else null }
            }.getOrNull()
            if (n != null) main.post { names[number] = n; calls = calls.map { if (it.number == number) it.copy(name = n) else it } }
        }
        return null
    }

    /** Rings on the tablet until answered, unless the phone already sends its own ringtone over the voice link. */
    private fun updateRing() {
        val want = calls.any { it.state == PhoneCall.INCOMING } && !audioOnTablet
        if (want && ring == null) {
            val uri = RingtoneManager.getActualDefaultRingtoneUri(ctx, RingtoneManager.TYPE_RINGTONE)
                ?: RingtoneManager.getActualDefaultRingtoneUri(ctx, RingtoneManager.TYPE_NOTIFICATION)
                ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM)
            ring = runCatching { RingtoneManager.getRingtone(ctx, uri) }.getOrNull()?.apply {
                audioAttributes = AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_NOTIFICATION_RINGTONE)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build()
                isLooping = true
                runCatching { play() }
            }
        } else if (!want) { ring?.stop(); ring = null }
    }

    /** An incoming call wakes the screen and brings Car Home over any app; that app comes back after the call. */
    @Suppress("DEPRECATION")
    private fun takeScreen() {
        val pm = ctx.getSystemService(PowerManager::class.java)
        if (!pm.isInteractive) runCatching { pm.newWakeLock(PowerManager.SCREEN_BRIGHT_WAKE_LOCK or PowerManager.ACQUIRE_CAUSES_WAKEUP, "carhome:call").acquire(10_000) }
        if (Apps.front != null) returnTo = Apps.front
        Apps.home(ctx)
    }

    // ------------------------------------------------------------------ actions

    fun answer() {
        val d = device() ?: return
        BtLink.call(hf, "acceptCall", d, if (calls.any { it.state == PhoneCall.ACTIVE }) ACCEPT_HOLD else ACCEPT_NONE)
    }
    fun decline() { device()?.let { BtLink.call(hf, "rejectCall", it) } }
    fun end(c: PhoneCall) { val d = device() ?: return; raw[c.id]?.let { BtLink.call(hf, "terminateCall", d, it) } }
    /** Moves the call's audio between the tablet and the phone (for a private conversation). */
    fun toggleAudio() { val d = device() ?: return; BtLink.call(hf, if (audioOnTablet) "disconnectAudio" else "connectAudio", d) }
    fun setMute(m: Boolean) { audio.isMicrophoneMute = m; muted = m }
    /** Call volume: the Bluetooth app passes the voice-call stream on to the audio HAL and the phone. */
    fun volume(up: Boolean) = audio.adjustStreamVolume(AudioManager.STREAM_VOICE_CALL, if (up) AudioManager.ADJUST_RAISE else AudioManager.ADJUST_LOWER, 0)

    /** Asks the phone for its contacts (the phone shows its own one-time consent). */
    @SuppressLint("MissingPermission")
    private fun syncContacts(d: BluetoothDevice) {
        val p = pbap ?: return
        if (p.getConnectionState(d) == BluetoothProfile.STATE_DISCONNECTED) { BtLink.call(p, "setConnectionPolicy", d, 100); BtLink.call(p, "connect", d) }
    }

    // ------------------------------------------------------------------ lifecycle

    private val rx = object : BroadcastReceiver() {
        override fun onReceive(c: Context, i: Intent) {
            if (i.action == HF_CONNECTION && i.getIntExtra(BluetoothProfile.EXTRA_STATE, 0) == BluetoothProfile.STATE_CONNECTED)
                i.getParcelableExtra<BluetoothDevice>(BluetoothDevice.EXTRA_DEVICE)?.let { syncContacts(it) }
            refresh()
        }
    }

    private val proxies = object : BluetoothProfile.ServiceListener {
        override fun onServiceConnected(profile: Int, proxy: BluetoothProfile) {
            if (profile == HEADSET_CLIENT) hf = proxy else pbap = proxy
            device()?.let { syncContacts(it) }
            refresh()
        }
        override fun onServiceDisconnected(profile: Int) {
            if (profile == HEADSET_CLIENT) hf = null else pbap = null
            refresh()
        }
    }

    fun start() {
        val a = BluetoothAdapter.getDefaultAdapter() ?: return
        ctx.registerReceiver(rx, IntentFilter().apply { listOf(HF_CONNECTION, HF_AUDIO, HF_CALL).forEach { addAction(it) } })
        a.getProfileProxy(ctx, proxies, HEADSET_CLIENT)
        a.getProfileProxy(ctx, proxies, PBAP_CLIENT)
    }

    fun stop() {
        runCatching { ctx.unregisterReceiver(rx) }
        ring?.stop(); ring = null
        val a = BluetoothAdapter.getDefaultAdapter()
        hf?.let { a?.closeProfileProxy(HEADSET_CLIENT, it) }; hf = null
        pbap?.let { a?.closeProfileProxy(PBAP_CLIENT, it) }; pbap = null
        lookups.shutdown()
    }

    companion object {
        const val HEADSET_CLIENT = 16   // BluetoothProfile.HEADSET_CLIENT (system API)
        const val PBAP_CLIENT = 17      // BluetoothProfile.PBAP_CLIENT (system API)
        const val HF_CONNECTION = "android.bluetooth.headsetclient.profile.action.CONNECTION_STATE_CHANGED"
        const val HF_AUDIO = "android.bluetooth.headsetclient.profile.action.AUDIO_STATE_CHANGED"
        const val HF_CALL = "android.bluetooth.headsetclient.profile.action.AG_CALL_CHANGED"
        const val AUDIO_CONNECTED = 2   // BluetoothHeadsetClient.STATE_AUDIO_CONNECTED
        const val ACCEPT_NONE = 0       // BluetoothHeadsetClient.CALL_ACCEPT_NONE
        const val ACCEPT_HOLD = 1       // answer a waiting call and hold the active one
    }
}
