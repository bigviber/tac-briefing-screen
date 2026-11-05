package com.carivi.relay.media

import android.content.ComponentName
import android.content.Context
import android.util.Log
import androidx.core.content.ContextCompat
import androidx.media3.common.MediaMetadata
import androidx.media3.common.Player
import androidx.media3.session.MediaBrowser
import androidx.media3.session.SessionToken
import com.carivi.relay.core.BtConstants
import com.google.common.util.concurrent.ListenableFuture
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * AndroidX Media3 MediaBrowser bound to
 * com.android.bluetooth/.avrcpcontroller.BluetoothMediaBrowserService.
 *
 * On SDK 30 that service hosts a MediaSessionCompat whose callbacks live in
 * AvrcpControllerStateMachine: onPlay()/onPause()/onSkipToNext() become AVRCP
 * pass-through commands to the iPhone, and onPrepare()/onPlay() call
 * A2dpSinkService.requestAudioFocus(device, true) - which is exactly the hook
 * Mode 1 uses to make the A2dpSinkStreamHandler render the decoded stream.
 * Metadata (title/artist/album) arrives through AVRCP GetElementAttributes and
 * is exposed as Media3 MediaMetadata.
 */
class NowPlayingController(private val context: Context) {
    private val tag = "CarRelay.NowPlaying"

    data class NowPlaying(
        val connected: Boolean = false,
        val title: String? = null,
        val artist: String? = null,
        val album: String? = null,
        val isPlaying: Boolean = false,
        val playbackState: Int = Player.STATE_IDLE,
        val durationMs: Long = 0L,
        val positionMs: Long = 0L,
        val error: String? = null
    )

    private val _state = MutableStateFlow(NowPlaying())
    val state: StateFlow<NowPlaying> = _state.asStateFlow()

    private var browserFuture: ListenableFuture<MediaBrowser>? = null
    private var browser: MediaBrowser? = null

    private val listener = object : Player.Listener {
        override fun onMediaMetadataChanged(mediaMetadata: MediaMetadata) = publish()
        override fun onIsPlayingChanged(isPlaying: Boolean) = publish()
        override fun onPlaybackStateChanged(playbackState: Int) = publish()
        override fun onPositionDiscontinuity(oldPosition: Player.PositionInfo, newPosition: Player.PositionInfo, reason: Int) = publish()
    }

    fun connect() {
        if (browserFuture != null) return
        val token = SessionToken(
            context,
            ComponentName(BtConstants.BLUETOOTH_PACKAGE, BtConstants.MEDIA_BROWSER_SERVICE)
        )
        val future = MediaBrowser.Builder(context, token).buildAsync()
        browserFuture = future
        future.addListener({
            try {
                val b = future.get()
                browser = b
                b.addListener(listener)
                Log.i(tag, "connected to BluetoothMediaBrowserService")
                publish()
            } catch (t: Throwable) {
                Log.w(tag, "MediaBrowser connect failed", t)
                _state.value = NowPlaying(connected = false, error = t.cause?.message ?: t.message)
                browserFuture = null
            }
        }, ContextCompat.getMainExecutor(context))
    }

    fun disconnect() {
        browser?.removeListener(listener)
        browserFuture?.let { MediaBrowser.releaseFuture(it) }
        browser = null
        browserFuture = null
        _state.value = NowPlaying()
    }

    /** Mode 1 hook: onPrepare() -> A2dpSinkService.requestAudioFocus(device, true). */
    fun prepare() {
        browser?.prepare() ?: Log.w(tag, "prepare(): browser not connected")
    }

    fun play() = browser?.play() ?: Unit
    fun pause() = browser?.pause() ?: Unit
    fun next() = browser?.seekToNext() ?: Unit
    fun previous() = browser?.seekToPrevious() ?: Unit

    fun togglePlayPause() {
        val b = browser ?: return
        if (b.isPlaying) b.pause() else {
            b.prepare()
            b.play()
        }
    }

    private fun publish() {
        val b = browser ?: run {
            _state.value = NowPlaying()
            return
        }
        val md = b.mediaMetadata
        _state.value = NowPlaying(
            connected = true,
            title = md.title?.toString(),
            artist = md.artist?.toString(),
            album = md.albumTitle?.toString(),
            isPlaying = b.isPlaying,
            playbackState = b.playbackState,
            durationMs = if (b.duration > 0) b.duration else 0L,
            positionMs = b.currentPosition.coerceAtLeast(0L)
        )
    }
}
