package com.local.carhome

import android.content.ComponentName
import android.content.Context
import android.media.browse.MediaBrowser
import android.media.session.MediaController
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import androidx.compose.runtime.Stable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue

/** One row of the phone's library: a folder to open, a track to play, or a folder that can also be played whole. */
data class LibItem(val id: String, val title: String, val subtitle: String, val folder: Boolean, val playable: Boolean)

/** A level on the way down; [auto] levels were entered on their own because they held a single folder. */
data class Crumb(val id: String, val title: String, val auto: Boolean)

/**
 * The connected phone's music library, read over Bluetooth (AVRCP browsing) and shown iPod style: folders to drill into,
 * tracks to play, nothing to do on the phone. The source is the Bluetooth app's own browse service (the AVRCP controller,
 * component enabled in root/reapply_root.sh): root -> phone -> its media players -> the player's folders (an iPhone's
 * Music app gives Playlists, Artists, Albums, Songs...). Folders load lazily: the service first answers "not yet", asks
 * the phone, then reports the children, so the subscription callback simply fires again.
 * Connected only while the Music screen shows and the home screen is started.
 */
@Stable
class PhoneLibrary(private val ctx: Context) {
    /** False when the Bluetooth browse service cannot be reached at all. */
    var available by mutableStateOf(true); private set
    var path by mutableStateOf<List<Crumb>>(emptyList()); private set
    /** Children per level id; a level that is missing has not been answered yet. */
    val levels = mutableStateMapOf<String, List<LibItem>>()
    /** True once the current level has waited long enough that the driver should be told. */
    var slow by mutableStateOf(false); private set

    /** Back goes up only from levels the driver opened, never into the single-folder levels skipped on the way in. */
    val canGoBack get() = path.size > 1 && !path.last().auto

    private val main = Handler(Looper.getMainLooper())
    private var browser: MediaBrowser? = null
    private var controller: MediaController? = null
    private var subscribed: String? = null
    private var started = false
    private var shown = false
    private var phone: String? = null
    private var openedAt = 0L
    private var topRetries = 0

    private val conn = object : MediaBrowser.ConnectionCallback() {
        override fun onConnected() {
            val b = browser ?: return
            available = true
            controller = runCatching { MediaController(ctx, b.sessionToken) }.getOrNull()
            if (path.isEmpty()) path = listOf(Crumb(b.root, "Music", auto = true))
            show(path.last().id, reload = true)
        }
        override fun onConnectionFailed() { available = false; browser = null }
        override fun onConnectionSuspended() { controller = null; subscribed = null }
    }

    private val sub = object : MediaBrowser.SubscriptionCallback() {
        override fun onChildrenLoaded(parentId: String, children: List<MediaBrowser.MediaItem>) {
            val list = children.map { m ->
                LibItem(m.mediaId ?: "", m.description.title?.toString().orEmpty(), m.description.subtitle?.toString().orEmpty(), m.isBrowsable, m.isPlayable)
            }
            if (parentId != path.lastOrNull()?.id) { levels[parentId] = list; return }
            val top = path.all { it.auto }
            if (list.isEmpty()) {
                // Right after a phone connects, browsing comes up a moment after the audio; a deep folder may answer
                // "empty" before the phone does. Ask again before telling the driver there is nothing.
                if (top && phone != null && topRetries < 5) { topRetries++; main.postDelayed({ show(parentId, reload = true) }, 3000); return }
                if (!top && SystemClock.elapsedRealtime() - openedAt < 2500) { main.postDelayed({ if (parentId !in levels) levels[parentId] = emptyList() }, 2500); return }
            }
            // Skip levels that hold a single folder (the phone itself, a phone with one music app) to land on the menu.
            val only = list.singleOrNull()
            if (top && only != null && only.folder) { path = path + Crumb(only.id, only.title, auto = true); show(only.id, reload = false); return }
            levels[parentId] = list
        }
        override fun onError(parentId: String) { levels[parentId] = emptyList() }
    }

    private val slowCheck = Runnable { if (path.lastOrNull()?.id !in levels) slow = true }

    // ------------------------------------------------------------------ navigation and playback

    fun open(item: LibItem) {
        if (!item.folder) return
        path = path + Crumb(item.id, item.title, auto = false)
        show(item.id, reload = false)
    }

    fun back() {
        if (!canGoBack) return
        path = path.dropLast(1)
        show(path.last().id, reload = false)
    }

    /** Plays a track, or a whole playable folder (album, playlist), on the phone. */
    fun play(item: LibItem) { controller?.transportControls?.playFromMediaId(item.id, null) }
    fun play() { controller?.transportControls?.play() }
    fun next() { controller?.transportControls?.skipToNext() }
    fun prev() { controller?.transportControls?.skipToPrevious() }

    /** Called with the connected phone's address; a different phone (or none) starts again from the top. */
    fun forPhone(address: String?) {
        if (address == phone) return
        phone = address
        levels.clear(); topRetries = 0
        val b = browser
        path = if (b != null && b.isConnected) listOf(Crumb(b.root, "Music", auto = true)) else emptyList()
        path.lastOrNull()?.let { show(it.id, reload = true) }
    }

    private fun show(id: String, reload: Boolean) {
        val b = browser?.takeIf { it.isConnected } ?: return
        openedAt = SystemClock.elapsedRealtime()
        slow = false
        main.removeCallbacks(slowCheck); main.postDelayed(slowCheck, 12_000)
        if (subscribed == id && !reload) return
        subscribed?.let { runCatching { b.unsubscribe(it) } }
        subscribed = id
        runCatching { b.subscribe(id, sub) }
    }

    // ------------------------------------------------------------------ lifecycle

    fun start() { started = true; sync() }
    fun stop() { started = false; sync() }
    fun setShown(v: Boolean) { shown = v; sync() }

    private fun sync() {
        if (started && shown) {
            if (browser == null) browser = MediaBrowser(ctx, SERVICE, conn, null).also { b ->
                runCatching { b.connect() }.onFailure { available = false; browser = null }
            }
        } else {
            browser?.let { b -> subscribed?.let { runCatching { b.unsubscribe(it) } }; runCatching { b.disconnect() } }
            browser = null; controller = null; subscribed = null
            main.removeCallbacksAndMessages(null)
        }
    }

    companion object {
        val SERVICE = ComponentName("com.android.bluetooth", "com.android.bluetooth.avrcpcontroller.BluetoothMediaBrowserService")
    }
}
