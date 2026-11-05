package com.local.carhome

import android.os.SystemClock
import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.produceState
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.delay

/** Decline / end: a soft coral, calm rather than alarm red. */
internal val Coral = Color(0xFFF07C7C)

/**
 * The call screen over everything: an incoming call (who, Answer / Decline), a call being placed, or a call in progress
 * (timer, Mute, where the audio is, volume, End). In a call it can be hidden to see the map; the rail then shows a
 * call button that brings it back.
 */
@Composable
fun CallSheet(calls: CallLink, onHide: () -> Unit) {
    val list = calls.calls
    val c = list.firstOrNull { it.ringing } ?: list.firstOrNull { it.state == PhoneCall.ACTIVE } ?: list.firstOrNull() ?: return
    Box(Modifier.fillMaxSize().background(Color(0xF7161719)).pointerInput(Unit) { detectTapGestures { } }) {
        if (!c.ringing) Box(Modifier.align(Alignment.TopEnd).padding(36.dp)) { Pill("Hide", dim = true, onTap = onHide) }
        Column(Modifier.fillMaxSize().padding(vertical = 56.dp), horizontalAlignment = Alignment.CenterHorizontally) {
            Spacer(Modifier.weight(1f))
            Box(Modifier.size(132.dp).clip(CircleShape).background(Color(0x337DBDF2)), contentAlignment = Alignment.Center) {
                val ini = initials(c.name)
                if (ini != null) Txt(ini, t(46f, Sky, FontWeight.SemiBold)) else Glyph(G.Phone, 56.dp, Sky)
            }
            Spacer(Modifier.height(28.dp))
            Txt(c.name ?: c.number.ifBlank { "Unknown caller" }, t(46f, TextHi, FontWeight.SemiBold, ls = -1f, center = true),
                Modifier.padding(horizontal = 80.dp), maxLines = 1, overflow = TextOverflow.Ellipsis)
            Spacer(Modifier.height(10.dp))
            CallStatus(calls, c)
            calls.phoneName?.let { Txt("via $it", t(16f, TextLo, center = true), Modifier.padding(top = 6.dp)) }
            Spacer(Modifier.weight(1f))
            if (c.ringing) Row(horizontalArrangement = Arrangement.spacedBy(160.dp)) {
                CallButton(G.Call, "Decline", 120.dp, Coral, Page) { calls.decline() }
                CallButton(G.Call, "Answer", 120.dp, Mint, Page) { calls.answer() }
            } else Row(horizontalArrangement = Arrangement.spacedBy(36.dp)) {
                CallButton(if (calls.muted) G.MicOff else G.Mic, if (calls.muted) "Muted" else "Mute", 96.dp,
                    if (calls.muted) Color(0x337DBDF2) else CardHi, if (calls.muted) Sky else TextHi) { calls.setMute(!calls.muted) }
                CallButton(G.Speaker, if (calls.audioOnTablet) "On tablet" else "On phone", 96.dp, CardHi, TextHi) { calls.toggleAudio() }
                CallButton(null, "Quieter", 96.dp, CardHi, TextHi, text = "\u2212") { calls.volume(false) }
                CallButton(null, "Louder", 96.dp, CardHi, TextHi, text = "+") { calls.volume(true) }
                CallButton(G.Call, "End", 96.dp, Coral, Page) { calls.end(c) }
            }
        }
    }
}

/** Incoming / calling / on hold, or the running time of the call (ticks once a second). */
@Composable
private fun CallStatus(calls: CallLink, c: PhoneCall) {
    val line = when (c.state) {
        PhoneCall.INCOMING -> "Incoming call" + if (c.name != null && c.number.isNotBlank()) "  \u00B7  " + c.number else ""
        PhoneCall.WAITING -> "Second call waiting"
        PhoneCall.DIALING, PhoneCall.ALERTING -> "Calling\u2026"
        PhoneCall.HELD -> "On hold"
        else -> {
            val secs by produceState(0L, c.since) { while (true) { value = if (c.since > 0) (SystemClock.elapsedRealtime() - c.since) / 1000 else 0; delay(1000) } }
            clock(secs * 1000) + if (calls.audioOnTablet) "" else "  \u00B7  audio on the phone"
        }
    }
    Txt(line, t(21f, if (c.ringing) Mint else TextMid, center = true, tabular = true))
}

@Composable
private fun CallButton(g: G?, label: String, size: Dp, bg: Color, fg: Color, text: String? = null, onTap: () -> Unit) {
    Column(horizontalAlignment = Alignment.CenterHorizontally) {
        PressCard(Modifier.size(size), radius = size / 2, color = bg, onTap = onTap) {
            if (g != null) Glyph(g, size * 0.4f, fg, Modifier.align(Alignment.Center))
            else Txt(text.orEmpty(), t(40f, fg, FontWeight.Light), Modifier.align(Alignment.Center))
        }
        Spacer(Modifier.height(12.dp))
        Txt(label, t(17f, TextMid, FontWeight.Medium))
    }
}

/** "Jane Smith" -> "JS", "Mom" -> "M"; null without a contact name. */
private fun initials(name: String?): String? =
    name?.split(' ')?.mapNotNull { w -> w.firstOrNull()?.takeIf { it.isLetter() }?.uppercaseChar() }?.take(2)?.joinToString("")?.ifEmpty { null }
