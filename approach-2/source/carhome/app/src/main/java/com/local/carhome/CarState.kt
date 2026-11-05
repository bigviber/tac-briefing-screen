package com.local.carhome

import android.annotation.SuppressLint
import android.content.BroadcastReceiver
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.location.GnssStatus
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.media.AudioManager
import android.media.MediaMetadata
import android.media.browse.MediaBrowser
import android.media.session.MediaController
import android.media.session.MediaSessionManager
import android.media.session.PlaybackState
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.wifi.WifiManager
import android.os.BatteryManager
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import androidx.compose.runtime.Stable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap

data class Fix(val speedMs: Float, val bearing: Float?, val accuracyM: Float)
/** [positionMs] was true at [positionAt] (elapsedRealtime) and advances at [rate] while playing; [durationMs] is 0 when unknown. */
data class NowPlaying(val title: String, val artist: String, val app: String, val pkg: String, val art: ImageBitmap?, val playing: Boolean,
                      val durationMs: Long, val positionMs: Long, val positionAt: Long, val rate: Float)

/**
 * Everything the home screen shows that comes from the car or the tablet, as Compose state. Started in onStart
 * and stopped in onStop, so GPS, Bluetooth and media listeners only run while the home screen is visible.
 */
@Stable
class CarState(private val ctx: Context) {
    private val main = Handler(Looper.getMainLooper())
    private val audio = ctx.getSystemService(AudioManager::class.java)
    private val lm = ctx.getSystemService(LocationManager::class.java)

    /** Current GPS fix, or null when there is none or the last one is older than 4 s (tunnel, garage). */
    var fix by mutableStateOf<Fix?>(null); private set
    var satsUsed by mutableIntStateOf(0); private set
    var satsSeen by mutableIntStateOf(0); private set
    var locationAllowed by mutableStateOf(true); private set
    var battery by mutableIntStateOf(-1); private set
    var charging by mutableStateOf(false); private set
    /** Wi-Fi network name when connected, null otherwise. */
    var wifi by mutableStateOf<String?>(null); private set
    var volume by mutableIntStateOf(0); private set
    val volumeMax: Int = audio.getStreamMaxVolume(AudioManager.STREAM_MUSIC)
    var volumeLocked by mutableStateOf(VolumeLock.locked(ctx)); private set
    var media by mutableStateOf<NowPlaying?>(null); private set
    val heading = Heading(ctx)

    // ---------- GPS ----------
    private val stale = Runnable { fix = null }
    private val locListener = object : LocationListener {
        override fun onLocationChanged(l: Location) {
            val course = if (l.hasBearing() && l.speed > 2f) l.bearing else null
            fix = Fix(if (l.hasSpeed()) l.speed else 0f, course, if (l.hasAccuracy()) l.accuracy else 99f)
            course?.let(heading::onCourse)
            main.removeCallbacks(stale); main.postDelayed(stale, 4000)
        }
        override fun onProviderEnabled(provider: String) {}
        override fun onProviderDisabled(provider: String) { fix = null }
        @Deprecated("old API") override fun onStatusChanged(provider: String?, status: Int, extras: Bundle?) {}
    }
    private val gnss = object : GnssStatus.Callback() {
        override fun onSatelliteStatusChanged(s: GnssStatus) {
            var used = 0
            for (i in 0 until s.satelliteCount) if (s.usedInFix(i)) used++
            satsUsed = used; satsSeen = s.satelliteCount
        }
    }

    @SuppressLint("MissingPermission")
    private fun startGps() {
        locationAllowed = ctx.checkSelfPermission(android.Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED
        if (!locationAllowed) return
        runCatching {
            lm.requestLocationUpdates(LocationManager.GPS_PROVIDER, 1000L, 0f, locListener, Looper.getMainLooper())
            lm.registerGnssStatusCallback(gnss, main)
        }
    }

    // ---------- battery, volume ----------
    private val rx = object : BroadcastReceiver() {
        override fun onReceive(c: Context, i: Intent) {
            when (i.action) {
                Intent.ACTION_BATTERY_CHANGED -> {
                    val lvl = i.getIntExtra(BatteryManager.EXTRA_LEVEL, -1)
                    battery = if (lvl < 0) -1 else lvl * 100 / i.getIntExtra(BatteryManager.EXTRA_SCALE, 100)
                    charging = i.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) != 0
                }
                VOLUME_CHANGED -> volume = audio.getStreamVolume(AudioManager.STREAM_MUSIC)
            }
        }
    }

    // ---------- Wi-Fi: event driven, no polling ----------
    private val cm = ctx.getSystemService(ConnectivityManager::class.java)
    private val net = object : ConnectivityManager.NetworkCallback() {
        override fun onCapabilitiesChanged(n: Network, caps: NetworkCapabilities) {
            if (!caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) return
            @Suppress("DEPRECATION")
            val ssid = ctx.getSystemService(WifiManager::class.java).connectionInfo?.ssid?.trim('"')
            main.post { wifi = ssid?.takeIf { it.isNotBlank() && it != "<unknown ssid>" } ?: "Wi-Fi" }
        }
        override fun onLost(n: Network) { main.post { wifi = null } }
    }

    // ---------- media (needs notification-listener access, granted once over ADB) ----------
    private val msm = ctx.getSystemService(MediaSessionManager::class.java)
    private val listener = ComponentName(ctx, MediaListener::class.java)
    private var controller: MediaController? = null
    private val mcb = object : MediaController.Callback() {
        override fun onMetadataChanged(metadata: MediaMetadata?) = publishMedia()
        override fun onPlaybackStateChanged(state: PlaybackState?) = publishMedia()
        override fun onSessionDestroyed() = pickSession()
    }
    private val sessions = MediaSessionManager.OnActiveSessionsChangedListener { pickSession() }

    // The phone's Bluetooth session often plays without being flagged active, so active-session lists miss it. It is
    // watched directly, through the session token of the Bluetooth browse service, and counts once it plays or pauses.
    private var btBrowser: MediaBrowser? = null
    private var btSession: MediaController? = null
    private val btcb = object : MediaController.Callback() {
        override fun onMetadataChanged(metadata: MediaMetadata?) = pickSession()
        override fun onPlaybackStateChanged(state: PlaybackState?) = pickSession()
    }
    private val btConn = object : MediaBrowser.ConnectionCallback() {
        override fun onConnected() {
            val b = btBrowser ?: return
            btSession?.unregisterCallback(btcb)
            btSession = runCatching { MediaController(ctx, b.sessionToken) }.getOrNull()?.also { it.registerCallback(btcb, main) }
            pickSession()
        }
        override fun onConnectionSuspended() { btSession?.unregisterCallback(btcb); btSession = null; pickSession() }
    }

    private fun pickSession() {
        val list = runCatching { msm.getActiveSessions(listener) }.getOrDefault(emptyList()).toMutableList()
        btSession?.let { bt ->
            val st = bt.playbackState?.state
            if (list.none { it.sessionToken == bt.sessionToken } && bt.metadata != null &&
                (st == PlaybackState.STATE_PLAYING || st == PlaybackState.STATE_PAUSED)) list.add(bt)
        }
        val best = list.firstOrNull { it.playbackState?.state == PlaybackState.STATE_PLAYING } ?: list.firstOrNull()
        if (best?.sessionToken != controller?.sessionToken) {
            controller?.unregisterCallback(mcb)
            controller = best
            best?.registerCallback(mcb, main)
        }
        publishMedia()
    }

    private var trackKey: String? = null
    private var trackArt: ImageBitmap? = null
    private var trackApp = ""

    private fun publishMedia() {
        val c = controller ?: run { media = null; return }
        val m = c.metadata
        val title = m?.getString(MediaMetadata.METADATA_KEY_TITLE) ?: m?.getString(MediaMetadata.METADATA_KEY_DISPLAY_TITLE) ?: ""
        val artist = m?.getString(MediaMetadata.METADATA_KEY_ARTIST) ?: m?.getString(MediaMetadata.METADATA_KEY_ALBUM_ARTIST) ?: ""
        val key = c.packageName + "|" + title + "|" + artist
        if (key != trackKey) {             // cover and app name are worked out once per track, not on every state change
            trackKey = key
            val bmp = m?.getBitmap(MediaMetadata.METADATA_KEY_ALBUM_ART) ?: m?.getBitmap(MediaMetadata.METADATA_KEY_ART)
            trackArt = bmp?.let { b -> (if (b.width > 320) Bitmap.createScaledBitmap(b, 320, 320 * b.height / b.width, true) else b).asImageBitmap() }
            trackApp = runCatching { ctx.packageManager.getApplicationLabel(ctx.packageManager.getApplicationInfo(c.packageName, 0)).toString() }.getOrDefault("")
        }
        val ps = c.playbackState
        media = NowPlaying(title, artist, trackApp, c.packageName, trackArt, ps?.state == PlaybackState.STATE_PLAYING,
            m?.getLong(MediaMetadata.METADATA_KEY_DURATION) ?: 0L, ps?.position ?: 0L, ps?.lastPositionUpdateTime ?: 0L, ps?.playbackSpeed ?: 1f)
    }

    fun playPause() { val c = controller ?: return; if (media?.playing == true) c.transportControls.pause() else c.transportControls.play() }
    fun next() { controller?.transportControls?.skipToNext() }
    fun prev() { controller?.transportControls?.skipToPrevious() }

    // ---------- volume ----------
    fun changeVolume(step: Int) {
        if (volumeLocked) return
        val v = step.coerceIn(0, volumeMax)
        if (v != volume) { audio.setStreamVolume(AudioManager.STREAM_MUSIC, v, 0); volume = v }
    }

    fun toggleLock() {
        if (volumeLocked) VolumeLock.unlock(ctx) else VolumeLock.lock(ctx, volume)
        volumeLocked = VolumeLock.locked(ctx)
    }

    /** After the volume panel changed the media lock. */
    fun syncLock() { volumeLocked = VolumeLock.locked(ctx); volume = audio.getStreamVolume(AudioManager.STREAM_MUSIC) }

    // ---------- lifecycle ----------
    fun start() {
        volume = audio.getStreamVolume(AudioManager.STREAM_MUSIC)
        volumeLocked = VolumeLock.locked(ctx)
        ctx.registerReceiver(rx, IntentFilter().apply {
            addAction(Intent.ACTION_BATTERY_CHANGED); addAction(VOLUME_CHANGED)
        })
        heading.start()
        startGps()
        wifi = null
        runCatching { cm.registerDefaultNetworkCallback(net, main) }
        runCatching { msm.addOnActiveSessionsChangedListener(sessions, listener, main) }
        btBrowser = MediaBrowser(ctx, PhoneLibrary.SERVICE, btConn, null).also { b -> runCatching { b.connect() } }
        pickSession()
    }

    fun stop() {
        runCatching { ctx.unregisterReceiver(rx) }
        runCatching { lm.removeUpdates(locListener) }
        heading.stop()
        runCatching { lm.unregisterGnssStatusCallback(gnss) }
        main.removeCallbacks(stale)
        runCatching { cm.unregisterNetworkCallback(net) }
        runCatching { msm.removeOnActiveSessionsChangedListener(sessions) }
        controller?.unregisterCallback(mcb); controller = null
        btSession?.unregisterCallback(btcb); btSession = null
        runCatching { btBrowser?.disconnect() }; btBrowser = null
    }

    companion object { const val VOLUME_CHANGED = "android.media.VOLUME_CHANGED_ACTION" }
}
