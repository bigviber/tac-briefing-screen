package com.local.carhome

import android.os.SystemClock
import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
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
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.delay

/**
 * The Bluetooth screen behind the Bluetooth buttons: the audio route at the top, then the phone and the car side by side,
 * each with its paired devices and its own way to pair. Nothing starts by itself: being visible lets any nearby phone
 * find the tablet, and scanning can make Bluetooth music stutter, so both wait for their button.
 */
@Composable
fun BtSheet(bt: BtLink, onClose: () -> Unit) {
    Column(Modifier.fillMaxSize().background(Color(0xF5161719)).pointerInput(Unit) { detectTapGestures { } }.padding(horizontal = 40.dp, vertical = 28.dp)) {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Txt("Bluetooth", t(40f, TextHi, FontWeight.SemiBold, ls = -1f), Modifier.weight(1f))
            RouteStrip(bt)
            Spacer(Modifier.width(28.dp))
            PressCard(Modifier.size(72.dp), radius = 36.dp, color = CardHi, onTap = onClose) { Glyph(G.Close, 28.dp, TextHi, Modifier.align(Alignment.Center)) }
        }
        bt.message?.let { Txt(it, t(15f, Amber), Modifier.padding(top = 6.dp)) }
        Spacer(Modifier.height(18.dp))
        Row(Modifier.fillMaxWidth().weight(1f), horizontalArrangement = Arrangement.spacedBy(20.dp)) {
            SidePanel(bt, Side.PHONE, Modifier.weight(1f).fillMaxHeight())
            SidePanel(bt, Side.CAR, Modifier.weight(1f).fillMaxHeight())
        }
    }
}

/** Phone -> tablet -> car at a glance. */
@Composable
private fun RouteStrip(bt: BtLink) {
    val phone = bt.phones.firstOrNull { it.connected }
    val car = bt.cars.firstOrNull { it.connected }
    Row(verticalAlignment = Alignment.CenterVertically) {
        RouteDot(G.Phone, phone != null)
        Glyph(G.Arrow, 18.dp, if (phone?.streaming == true) Sky else TextLo, Modifier.padding(horizontal = 6.dp))
        RouteDot(G.Nav, bt.on)
        Glyph(G.Arrow, 18.dp, if (car != null) Sky else TextLo, Modifier.padding(horizontal = 6.dp))
        RouteDot(G.Car, car != null)
    }
}

@Composable
private fun RouteDot(g: G, active: Boolean) {
    Box(Modifier.size(38.dp).clip(CircleShape).background(if (active) Color(0x337DBDF2) else CardHi), contentAlignment = Alignment.Center) {
        Glyph(g, 20.dp, if (active) Sky else TextLo)
    }
}

@Composable
private fun SidePanel(bt: BtLink, side: Side, modifier: Modifier) {
    val isPhone = side == Side.PHONE
    val paired = if (isPhone) bt.phones else bt.cars
    Column(modifier.clip(PanelShape).background(Panel).padding(24.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Box(Modifier.size(56.dp).clip(CircleShape).background(Color(0x337DBDF2)), contentAlignment = Alignment.Center) {
                Glyph(if (isPhone) G.Phone else G.Car, 28.dp, Sky)
            }
            Spacer(Modifier.width(16.dp))
            Column {
                Txt(if (isPhone) "Phone" else "Car", t(26f, TextHi, FontWeight.SemiBold))
                Txt(if (isPhone) "Music and calls come from here" else "The tablet's audio goes here", t(15f, TextMid))
            }
        }
        Spacer(Modifier.height(18.dp))
        LazyColumn(Modifier.weight(1f).fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            if (paired.isEmpty()) item { Txt(if (isPhone) "No phone paired yet." else "No car paired yet.", t(17f, TextLo)) }
            items(paired, key = { "p" + it.address }) { d -> DeviceRow(bt, d) }
            if (!isPhone && (bt.scanning || bt.found.isNotEmpty())) {
                item { Txt("NEARBY", t(13f, TextLo, FontWeight.SemiBold, ls = 1.5f), Modifier.padding(top = 12.dp, bottom = 2.dp)) }
                if (bt.found.isEmpty()) item { Txt("Searching\u2026", t(16f, TextLo)) }
                items(bt.found, key = { "f" + it.address }) { d -> DeviceRow(bt, d) }
            }
        }
        Spacer(Modifier.height(14.dp))
        if (isPhone) PhonePairing(bt) else CarPairing(bt)
    }
}

@Composable
private fun PhonePairing(bt: BtLink) {
    val left by produceState(0L, bt.visibleUntil) {
        while (true) { value = ((bt.visibleUntil - SystemClock.elapsedRealtime()) / 1000).coerceAtLeast(0); if (value == 0L) break; delay(1000) }
    }
    PairingFooter(left > 0,
        if (left > 0) "Visible  %d:%02d".format(left / 60, left % 60) else "Not visible to phones",
        "On your phone, open Bluetooth, choose \u201CLenovo Tab P11\u201D and confirm the code.",
        "Pair a phone") { bt.startPhonePairing() }
}

@Composable
private fun CarPairing(bt: BtLink) {
    PairingFooter(bt.scanning, if (bt.scanning) "Searching" else "Not searching",
        "On the Verano, open Phone / Bluetooth and choose Pair or Add device. Pick it above, then confirm the same code on the car.",
        "Pair a car") { bt.startCarScan() }
}

/** Status dot, the action, and how-to text that only shows while pairing is under way. */
@Composable
private fun PairingFooter(active: Boolean, status: String, howTo: String, action: String, onTap: () -> Unit) {
    if (active) Txt(howTo, t(16f, TextMid), Modifier.padding(bottom = 12.dp))
    Row(verticalAlignment = Alignment.CenterVertically) {
        Box(Modifier.size(12.dp).clip(CircleShape).background(if (active) Mint else TextLo))
        Spacer(Modifier.width(10.dp))
        Txt(status, t(17f, TextHi, FontWeight.Medium, tabular = true), Modifier.weight(1f))
        Pill(action, onTap = onTap)
    }
}

@Composable
private fun DeviceRow(bt: BtLink, d: BtDev) {
    PressCard(Modifier.fillMaxWidth().height(80.dp), radius = 20.dp, color = Card, onTap = if (!d.bonded && !d.pairing) ({ bt.pair(d.address, d.side) }) else null) {
        Row(Modifier.fillMaxSize().padding(horizontal = 18.dp), verticalAlignment = Alignment.CenterVertically) {
            Glyph(if (d.side == Side.PHONE) G.Phone else G.Car, 24.dp, if (d.connected) Sky else TextMid)
            Spacer(Modifier.width(16.dp))
            Column(Modifier.weight(1f)) {
                Txt(d.name, t(18f, TextHi, FontWeight.Medium), maxLines = 1, overflow = TextOverflow.Ellipsis)
                Txt(when {
                    d.pairing -> "Pairing\u2026 confirm the code on the other device"
                    !d.bonded -> "Tap to pair"
                    d.streaming -> if (d.side == Side.PHONE) "Streaming to the tablet" else "Playing"
                    d.connected -> "Connected"
                    else -> "Paired, not connected"
                }, t(14f, if (d.connected) Sky else TextLo), maxLines = 1, overflow = TextOverflow.Ellipsis)
            }
            if (d.bonded) {
                if (d.connected) Pill("Disconnect") { bt.disconnect(d.address, d.side) } else Pill("Connect") { bt.connect(d.address, d.side) }
                Spacer(Modifier.width(8.dp))
                Pill("Forget", dim = true) { bt.forget(d.address) }
            }
        }
    }
}

@Composable
internal fun Pill(label: String, dim: Boolean = false, onTap: () -> Unit) {
    PressCard(Modifier.height(44.dp), radius = 22.dp, color = if (dim) CardHi else Color(0x337DBDF2), onTap = onTap) {
        Txt(label, t(15f, if (dim) TextMid else Sky, FontWeight.SemiBold), Modifier.align(Alignment.Center).padding(horizontal = 18.dp))
    }
}
