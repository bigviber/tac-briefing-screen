package com.local.carhome

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.media.AudioManager
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.Stable
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import kotlin.math.roundToInt

/** One sound the driver can set and hold. */
data class VolChannel(val stream: Int, val label: String, val detail: String, val glyph: G)

/**
 * Live levels and locks for the volume panel. Android ties navigation voice and the Assistant to the media volume on a
 * tablet, so they share the Media slider; calls (the caller's voice), ringtone and alarms are separate.
 */
@Stable
class VolumeMixer(private val ctx: Context, private val onMediaLock: () -> Unit) {
    private val audio = ctx.getSystemService(AudioManager::class.java)
    val channels = listOf(
        VolChannel(AudioManager.STREAM_MUSIC, "Media", "Music, navigation voice and Assistant", G.Note),
        VolChannel(AudioManager.STREAM_VOICE_CALL, "Calls", "The caller's voice", G.Call),
        VolChannel(AudioManager.STREAM_RING, "Ringtone & alerts", "Incoming calls and notifications", G.Bell),
        VolChannel(AudioManager.STREAM_ALARM, "Alarms", "Alarms and timers", G.Alarm),
    )
    val max = channels.associate { it.stream to audio.getStreamMaxVolume(it.stream) }
    private val min = channels.associate { it.stream to runCatching { audio.getStreamMinVolume(it.stream) }.getOrDefault(0) }
    val level = mutableStateMapOf<Int, Int>()
    val locked = mutableStateMapOf<Int, Boolean>()

    private fun refresh() = channels.forEach { level[it.stream] = audio.getStreamVolume(it.stream); locked[it.stream] = VolumeLock.locked(ctx, it.stream) }

    fun set(stream: Int, v: Int) {
        if (locked[stream] == true) return
        val want = v.coerceIn(min[stream] ?: 0, max[stream] ?: 0)
        if (want != level[stream]) runCatching { audio.setStreamVolume(stream, want, 0) }   // ringtone to 0 may need Do Not Disturb access
        level[stream] = audio.getStreamVolume(stream)
    }

    fun toggleLock(stream: Int) {
        if (locked[stream] == true) VolumeLock.unlock(ctx, stream) else VolumeLock.lock(ctx, audio.getStreamVolume(stream), stream)
        locked[stream] = VolumeLock.locked(ctx, stream)
        if (stream == AudioManager.STREAM_MUSIC) onMediaLock()
    }

    private val rx = object : BroadcastReceiver() { override fun onReceive(c: Context, i: Intent) = refresh() }
    fun start() { refresh(); ctx.registerReceiver(rx, IntentFilter(CarState.VOLUME_CHANGED)) }
    fun stop() { runCatching { ctx.unregisterReceiver(rx) } }
}

/** Slides out from the rail: one row per sound with a big slider, its level and a lock. */
@Composable
fun VolumePanel(mixer: VolumeMixer, modifier: Modifier) {
    DisposableEffect(mixer) { mixer.start(); onDispose { mixer.stop() } }
    Column(modifier.clip(PanelShape).background(Panel).padding(horizontal = 28.dp, vertical = 26.dp), verticalArrangement = Arrangement.spacedBy(26.dp)) {
        Column {
            Txt("Volume", t(30f, TextHi, FontWeight.SemiBold, ls = -0.5f))
            Txt("Set each sound, lock it to keep it there", t(15f, TextMid))
        }
        for (ch in mixer.channels) ChannelRow(mixer, ch)
    }
}

@Composable
private fun ChannelRow(mixer: VolumeMixer, ch: VolChannel) {
    val value = mixer.level[ch.stream] ?: 0
    val max = mixer.max[ch.stream] ?: 1
    val locked = mixer.locked[ch.stream] == true
    Column(Modifier.fillMaxWidth()) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Box(Modifier.size(44.dp).clip(CircleShape).background(CardHi), contentAlignment = Alignment.Center) { Glyph(ch.glyph, 20.dp, if (locked) Sky else TextMid) }
            Spacer(Modifier.width(14.dp))
            Column(Modifier.weight(1f)) {
                Txt(ch.label, t(19f, TextHi, FontWeight.Medium))
                Txt(if (locked) "Held at this level" else ch.detail, t(14f, if (locked) Sky else TextMid), maxLines = 1)
            }
            Txt("${(value * 100f / max.coerceAtLeast(1)).roundToInt()}%", t(20f, TextHi, FontWeight.Medium, tabular = true), Modifier.padding(horizontal = 12.dp))
            PressCard(Modifier.size(48.dp), radius = 24.dp, color = if (locked) Color(0x337DBDF2) else CardHi, onTap = { mixer.toggleLock(ch.stream) }) {
                Glyph(if (locked) G.Lock else G.Unlock, 20.dp, if (locked) Sky else TextMid, Modifier.align(Alignment.Center))
            }
        }
        Spacer(Modifier.height(10.dp))
        VolumeBar(value, max, locked, Modifier.fillMaxWidth().height(46.dp)) { mixer.set(ch.stream, it) }
    }
}
