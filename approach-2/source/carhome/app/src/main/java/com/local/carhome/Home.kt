package com.local.carhome

import android.content.Context
import android.content.Intent
import android.provider.Settings
import android.os.SystemClock
import android.text.format.DateFormat
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.Crossfade
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.scaleIn
import androidx.compose.animation.scaleOut
import androidx.compose.animation.slideInHorizontally
import androidx.compose.animation.slideOutHorizontally
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.detectHorizontalDragGestures
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
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.produceState
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.clipToBounds
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.activity.compose.BackHandler
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlin.math.roundToInt

private const val CAR_NAME = "Buick Verano"
internal val PanelShape = RoundedCornerShape(32.dp)

@Composable
fun CarHome(car: CarState, bt: BtLink, lib: PhoneLibrary, calls: CallLink, apps: List<AppEntry>) {
    val ctx = LocalContext.current
    val labels = remember(apps) { apps.associate { it.pkg to it.label } }
    val installed = labels.keys
    var drawer by remember { mutableStateOf(false) }
    var navPicker by remember { mutableStateOf(false) }
    var btOpen by remember { mutableStateOf(false) }
    val openBluetooth: () -> Unit = { Apps.home(ctx); btOpen = true }
    val closeBluetooth: () -> Unit = { bt.stopPairing(); btOpen = false }
    BackHandler(btOpen, closeBluetooth)
    val navPkg = remember(installed, navPicker) { Apps.nav(ctx, installed) }
    val openNav: () -> Unit = { if (navPkg != null) Apps.launch(ctx, navPkg) else navPicker = true }
    // The Music screen takes the dashboard's place beside the rail; the navigation bar's Back goes up a level, then home.
    var music by remember { mutableStateOf(false) }
    BackHandler(music) { if (lib.canGoBack) lib.back() else music = false }
    // The volume panel slides out over the dashboard; an app that was in front comes back when it closes.
    var volumeOpen by remember { mutableStateOf(false) }
    var volumeReturn by remember { mutableStateOf<String?>(null) }
    val mixer = remember { VolumeMixer(ctx.applicationContext) { car.syncLock() } }
    val closeVolume: () -> Unit = { volumeOpen = false; volumeReturn?.let { Apps.launch(ctx, it) }; volumeReturn = null }
    BackHandler(volumeOpen) { closeVolume() }
    // A call takes the whole screen; while it is in progress it can be hidden, and a new incoming call always shows again.
    val inCall = calls.calls.isNotEmpty()
    var callHidden by remember { mutableStateOf(false) }
    val ringingId = calls.calls.firstOrNull { it.ringing }?.id
    LaunchedEffect(ringingId, inCall) { if (ringingId != null || !inCall) callHidden = false }
    BackHandler(inCall && !callHidden) { if (ringingId == null) callHidden = true }

    // The music card only exists while music plays; after a pause it stays 60 s so it can be resumed, then gives way to Sound.
    var showMusic by remember { mutableStateOf(false) }
    val media = car.media
    // Show the player card whenever a connected phone's Bluetooth session is current, not only when it reports
    // "playing": during real playback that session can sit at a stale state (state=7, actions=0) until AVRCP event
    // registration completes, and the old `playing`-only gate kept the Sound card up over the music.
    val phoneBtOn = bt.phones.any { it.connected }
    LaunchedEffect(media?.playing, media == null, car.mediaActive && phoneBtOn) {
        when {
            media?.playing == true || (car.mediaActive && phoneBtOn) -> showMusic = true
            media == null -> showMusic = false
            else -> { delay(60_000); showMusic = false }
        }
    }

    Box(Modifier.fillMaxSize().background(Page)) {
        Row(Modifier.fillMaxSize().padding(20.dp)) {
            // A music app on the tablet itself (phone audio comes from the Bluetooth app, which has no screen to open).
            val musicPkg = media?.pkg?.takeIf { it in installed } ?: Apps.music(installed)
            Rail(Apps.front, navPkg, musicPkg, music, volumeOpen, onCall = if (inCall && callHidden) ({ Apps.home(ctx); callHidden = false }) else null,
                onVolume = { if (volumeOpen) closeVolume() else { volumeReturn = Apps.front; Apps.home(ctx); volumeOpen = true } },
                onHome = { Apps.home(ctx); music = false; volumeOpen = false; volumeReturn = null },
                onApps = { Apps.home(ctx); drawer = true },
                onNav = openNav,
                onMusic = { Apps.home(ctx); music = true },
                onSettings = { Apps.launch(ctx, SETTINGS) })
            Column(Modifier.weight(1f).fillMaxHeight().padding(start = 12.dp)) {
                Header(car, bt, openBluetooth)
                Spacer(Modifier.height(14.dp))
                Crossfade(music, Modifier.fillMaxWidth().weight(1f), tween(220), label = "screen") { m ->
                    if (m) MusicScreen(car, bt, lib, musicPkg, musicPkg?.let { labels[it] }, Modifier.fillMaxSize(), onPairPhone = { openBluetooth(); bt.startPhonePairing() })
                    else Row(Modifier.fillMaxSize(), horizontalArrangement = Arrangement.spacedBy(16.dp)) {
                        CarPanel(car, bt.phones.any { it.connected } || bt.cars.any { it.connected }, Modifier.width(392.dp).fillMaxHeight(), openBluetooth)
                        Column(Modifier.weight(1f).fillMaxHeight().clip(PanelShape).background(Panel).padding(18.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
                            MapCard(car, navPkg?.let { labels[it] }, navPkg, Modifier.fillMaxWidth().weight(1f), onTap = openNav, onLong = { navPicker = true })
                            Row(Modifier.fillMaxWidth().height(262.dp), horizontalArrangement = Arrangement.spacedBy(16.dp)) {
                                CompassCard(car.heading, Modifier.weight(1.3f).fillMaxHeight())
                                Crossfade(showMusic && media != null, Modifier.weight(1f).fillMaxHeight(), label = "music") { playing ->
                                    if (playing && media != null) MusicCard(car, media, Modifier.fillMaxSize()) { music = true } else SoundCard(car, Modifier.fillMaxSize())
                                }
                            }
                        }
                    }
                }
            }
        }
        AnimatedVisibility(volumeOpen, enter = fadeIn(tween(200)), exit = fadeOut(tween(160))) {
            Box(Modifier.fillMaxSize().padding(start = RAIL_DP.dp).background(Color(0x990E0F11)).pointerInput(Unit) { detectTapGestures { closeVolume() } })
        }
        Box(Modifier.fillMaxHeight().padding(start = (RAIL_DP + 4).dp, top = 20.dp, bottom = 20.dp).clipToBounds()) {   // slides out from under the rail
            AnimatedVisibility(volumeOpen, enter = slideInHorizontally(tween(280)) { -it } + fadeIn(tween(200)),
                exit = slideOutHorizontally(tween(220)) { -it } + fadeOut(tween(180))) {
                VolumePanel(mixer, Modifier.width(540.dp).fillMaxHeight())
            }
        }
        AnimatedVisibility(drawer, enter = fadeIn(tween(180)) + scaleIn(tween(220), initialScale = 0.98f), exit = fadeOut(tween(150)) + scaleOut(tween(150), targetScale = 0.98f)) {
            Drawer(apps, onClose = { drawer = false }, onOpen = { drawer = false; Apps.launch(ctx, it) })
        }
        AnimatedVisibility(btOpen, enter = fadeIn(tween(180)) + scaleIn(tween(220), initialScale = 0.98f), exit = fadeOut(tween(150))) {
            BtSheet(bt, onClose = closeBluetooth)
        }
        AnimatedVisibility(navPicker, enter = fadeIn(tween(180)), exit = fadeOut(tween(150))) {
            NavPicker(Apps.navCandidates(installed), { labels[it] ?: it }, navPkg, onPick = { Apps.setNav(ctx, it); navPicker = false }, onClose = { navPicker = false })
        }
        AnimatedVisibility(inCall && !callHidden, enter = fadeIn(tween(180)) + scaleIn(tween(220), initialScale = 0.98f), exit = fadeOut(tween(150))) {
            CallSheet(calls, onHide = { callHidden = true })
        }
    }
}

// ------------------------------------------------------------------ rail + header

/**
 * The rail stays on screen beside every app window, like CarPlay's sidebar: the button of whatever is in front is lit,
 * Home brings the dashboard back, and the clock moves to the top of the rail while an app covers the header.
 */
@Composable
private fun Rail(front: String?, navPkg: String?, musicPkg: String?, music: Boolean, volume: Boolean, onCall: (() -> Unit)?, onVolume: () -> Unit, onHome: () -> Unit, onApps: () -> Unit, onNav: () -> Unit, onMusic: () -> Unit, onSettings: () -> Unit) {
    val ctx = LocalContext.current
    Column(Modifier.width(88.dp).fillMaxHeight(), horizontalAlignment = Alignment.CenterHorizontally) {
        Box(Modifier.size(64.dp), contentAlignment = Alignment.Center) {
            when {
                onCall != null -> PressCard(Modifier.size(64.dp), radius = 32.dp, color = Color(0x3386E3A6), onTap = onCall) {   // back to the call
                    Glyph(G.Call, 30.dp, Mint, Modifier.align(Alignment.Center))
                }
                front == null -> Glyph(G.Nav, 34.dp, Sky)
                else -> Txt(clockText(ctx, minuteTick()), t(21f, TextHi, FontWeight.SemiBold, tabular = true))
            }
        }
        Spacer(Modifier.weight(1f))
        Column(verticalArrangement = Arrangement.spacedBy(22.dp)) {
            RailButton(G.Home, front == null && !music && !volume, onHome)
            RailButton(G.Grid, front != null && front != navPkg && front != musicPkg && front != SETTINGS, onApps)
            RailButton(G.Nav, front != null && front == navPkg, onNav)
            RailButton(G.Note, !volume && if (front == null) music else front == musicPkg, onMusic)
            RailButton(G.Speaker, volume, onVolume)
            RailButton(G.Gear, front == SETTINGS, onSettings)
        }
        Spacer(Modifier.weight(1f))
    }
}

@Composable
private fun RailButton(g: G, selected: Boolean, onTap: () -> Unit) {
    PressCard(Modifier.size(72.dp), radius = 36.dp, color = if (selected) CardHi else Panel, onTap = onTap) {
        Glyph(g, 26.dp, if (selected) TextHi else TextMid, Modifier.align(Alignment.Center))
    }
}

@Composable
private fun Header(car: CarState, link: BtLink, onBluetooth: () -> Unit) {
    val ctx = LocalContext.current
    val now = minuteTick()
    Row(Modifier.fillMaxWidth().height(80.dp), verticalAlignment = Alignment.CenterVertically) {
        Column(Modifier.weight(1f)) {
            Txt(clockText(ctx, now), t(40f, TextHi, FontWeight.SemiBold, ls = -1f, tabular = true))
            Txt(SimpleDateFormat("EEEE  |  MMMM d, yyyy", Locale.getDefault()).format(now), t(16f, TextMid))
        }
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp), verticalAlignment = Alignment.CenterVertically) {
            val wifi = car.wifi
            val bt = (link.phones.firstOrNull { it.connected } ?: link.cars.firstOrNull { it.connected })?.name
            StatusBlock({ Glyph(G.Wifi, 20.dp, if (wifi != null) Sky else TextLo) }, wifi ?: "Not connected", "Wi-Fi") { openSettings(ctx, Settings.ACTION_WIFI_SETTINGS) }
            StatusBlock({ Glyph(G.Bluetooth, 20.dp, if (bt != null) Sky else TextLo) }, bt ?: "Not connected", "Bluetooth", onBluetooth)
            if (car.battery >= 0) StatusBlock({ BatteryGlyph(car.battery / 100f, car.charging, 24.dp) }, "${car.battery}%", if (car.charging) "Charging" else "Battery") {
                openSettings(ctx, Intent.ACTION_POWER_USAGE_SUMMARY)
            }
        }
    }
}

private const val SETTINGS = "com.android.settings"
private fun openSettings(ctx: Context, action: String) { Apps.open(ctx, Intent(action), SETTINGS) }

/** Current time, refreshed on the minute only: nothing that shows the clock recomposes in between. */
@Composable
private fun minuteTick(): Date {
    val now by produceState(Date()) { while (true) { delay(60_000L - System.currentTimeMillis() % 60_000L); value = Date() } }
    return now
}

private fun clockText(ctx: Context, d: Date) = SimpleDateFormat(if (DateFormat.is24HourFormat(ctx)) "HH:mm" else "h:mm", Locale.getDefault()).format(d)

@Composable
private fun StatusBlock(icon: @Composable () -> Unit, value: String, caption: String, onTap: () -> Unit) {
    PressCard(radius = 23.dp, color = Color.Transparent, onTap = onTap) { Row(Modifier.padding(horizontal = 10.dp, vertical = 6.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(12.dp)) {
        Box(Modifier.size(46.dp).clip(CircleShape).background(Panel), contentAlignment = Alignment.Center) { icon() }
        Column {
            Txt(value, t(17f, TextHi, FontWeight.Medium, tabular = true), Modifier.width(140.dp), maxLines = 1, overflow = TextOverflow.Ellipsis)
            Txt(caption, t(13f, TextMid))
        }
    } }
}

// ------------------------------------------------------------------ car panel: name, car, three rings, speedometer

@Composable
private fun CarPanel(car: CarState, btConnected: Boolean, modifier: Modifier, onBluetooth: () -> Unit) {
    val ctx = LocalContext.current
    val prefs = remember { ctx.getSharedPreferences("ui", Context.MODE_PRIVATE) }
    var mph by remember { mutableStateOf(prefs.getBoolean("mph", false)) }
    Column(modifier.clip(PanelShape).background(Panel).padding(horizontal = 22.dp, vertical = 20.dp), horizontalAlignment = Alignment.CenterHorizontally) {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Txt(CAR_NAME, t(26f, TextHi, FontWeight.Medium), Modifier.weight(1f))
            PressCard(Modifier.size(48.dp), radius = 24.dp, onTap = onBluetooth) {
                Glyph(G.Bluetooth, 20.dp, if (btConnected) Sky else TextLo, Modifier.align(Alignment.Center))
            }
        }
        CarArt(Modifier.fillMaxWidth(0.86f).height(176.dp))
        Spacer(Modifier.height(6.dp))
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            Box(Modifier.pointerInput(Unit) { detectTapGestures { openSettings(ctx, Intent.ACTION_POWER_USAGE_SUMMARY) } }) {
                RingGauge(car.battery.coerceAtLeast(0) / 100f, Mint, G.Bolt, "${car.battery.coerceAtLeast(0)}%", "Battery", 108.dp)
            }
            Box(Modifier.pointerInput(Unit) { detectTapGestures { car.toggleLock() } }) {   // tap locks / unlocks media volume
                RingGauge(car.volume / car.volumeMax.coerceAtLeast(1).toFloat(), Sky, if (car.volumeLocked) G.Lock else G.Speaker,
                    "${(car.volume * 100f / car.volumeMax.coerceAtLeast(1)).roundToInt()}%", if (car.volumeLocked) "Locked" else "Volume", 108.dp)
            }
            Box(Modifier.pointerInput(Unit) { detectTapGestures { openSettings(ctx, Settings.ACTION_LOCATION_SOURCE_SETTINGS) } }) {
                RingGauge(car.satsUsed / 12f, if (car.fix != null) Sky else TextLo, G.Gps, "${car.satsUsed}", "GPS", 108.dp)
            }
        }
        Box(Modifier.fillMaxWidth().weight(1f).pointerInput(Unit) { detectTapGestures { mph = !mph; prefs.edit().putBoolean("mph", mph).apply() } },
            contentAlignment = Alignment.Center) {
            val speed = car.fix?.let { (it.speedMs * if (mph) 2.23694f else 3.6f).let { s -> if (s < 1.5f) 0 else s.roundToInt() } }
            Speedometer(speed, if (mph) 120 else 180, if (mph) "mph" else "km/h", Modifier.fillMaxHeight())
        }
    }
}

// ------------------------------------------------------------------ sound (shown when nothing plays) and music

@Composable
private fun SoundCard(car: CarState, modifier: Modifier) {
    val locked = car.volumeLocked
    Column(modifier.clip(RoundedCornerShape(28.dp)).background(Card).padding(20.dp)) {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Txt("Sound", t(20f, TextHi, FontWeight.Medium), Modifier.weight(1f))
            PressCard(Modifier.size(width = 124.dp, height = 46.dp), radius = 23.dp, color = if (locked) Color(0x337DBDF2) else CardHi, onTap = { car.toggleLock() }) {
                Row(Modifier.align(Alignment.Center), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Glyph(if (locked) G.Lock else G.Unlock, 20.dp, if (locked) Sky else TextMid)
                    Txt(if (locked) "Locked" else "Lock", t(15f, if (locked) Sky else TextMid, FontWeight.SemiBold))
                }
            }
        }
        Spacer(Modifier.weight(1f))
        Row(verticalAlignment = Alignment.Bottom) {
            Txt("${(car.volume * 100f / car.volumeMax.coerceAtLeast(1)).roundToInt()}", t(64f, TextHi, FontWeight.SemiBold, ls = -2f, tabular = true))
            Txt("%", t(26f, TextMid, FontWeight.Medium), Modifier.padding(start = 4.dp, bottom = 12.dp))
            Spacer(Modifier.weight(1f))
            Txt(if (locked) "Held at this level" else "Media volume", t(15f, TextMid), Modifier.padding(bottom = 14.dp))
        }
        Spacer(Modifier.height(12.dp))
        VolumeBar(car.volume, car.volumeMax, locked, Modifier.fillMaxWidth().height(52.dp)) { car.changeVolume(it) }
    }
}

/** A thick, finger-friendly slider; drag or tap anywhere on it. Dimmed and inert while the lock is on. */
@Composable
internal fun VolumeBar(value: Int, max: Int, locked: Boolean, modifier: Modifier, onSet: (Int) -> Unit) {
    val frac by animateFloatAsState(if (max > 0) value.toFloat() / max else 0f, tween(120), label = "vol")
    fun stepAt(x: Float, w: Int) = ((x / w).coerceIn(0f, 1f) * max).roundToInt()
    Canvas(modifier
        .pointerInput(locked, max) { if (!locked) detectTapGestures { onSet(stepAt(it.x, size.width)) } }
        .pointerInput(locked, max) { if (!locked) detectHorizontalDragGestures { change, _ -> onSet(stepAt(change.position.x, size.width)) } }) {
        val r = CornerRadius(size.height / 2)
        drawRoundRect(Track, size = size, cornerRadius = r)
        val w = (size.width * frac).coerceAtLeast(size.height)
        drawRoundRect(if (locked) Color(0xFF55575E) else Sky, size = Size(w, size.height), cornerRadius = r)
        drawCircle(Color.White.copy(alpha = if (locked) 0.35f else 0.95f), size.height * 0.30f, Offset(w - size.height / 2, size.height / 2))
    }
}

@Composable
private fun MusicCard(car: CarState, m: NowPlaying, modifier: Modifier, onOpen: () -> Unit) {
    val pos = livePosition(m)
    PressCard(modifier, onTap = onOpen) {
        Column(Modifier.fillMaxSize().padding(16.dp)) {
            Cover(m.art, Modifier.fillMaxWidth().height(78.dp))
            Spacer(Modifier.height(10.dp))
            Txt(m.title.ifBlank { m.app }, t(20f, TextHi, FontWeight.SemiBold), maxLines = 1, overflow = TextOverflow.Ellipsis)
            Txt(m.artist.ifBlank { m.app }, t(15f, TextMid), maxLines = 1, overflow = TextOverflow.Ellipsis)
            Spacer(Modifier.height(8.dp))
            if (m.durationMs > 0) {
                Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    Txt(clock(pos), t(12f, TextMid, tabular = true))
                    Box(Modifier.weight(1f).padding(horizontal = 10.dp).height(3.dp).clip(RoundedCornerShape(2.dp)).background(Track)) {
                        Box(Modifier.fillMaxWidth((pos.toFloat() / m.durationMs).coerceIn(0f, 1f)).fillMaxHeight().background(Sky))
                    }
                    Txt(clock(m.durationMs), t(12f, TextMid, tabular = true))
                }
            }
            Spacer(Modifier.weight(1f))
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceEvenly, verticalAlignment = Alignment.CenterVertically) {
                RoundButton(G.Prev, 54.dp) { car.prev() }
                RoundButton(if (m.playing) G.Pause else G.Play, 64.dp, accent = true) { car.playPause() }
                RoundButton(G.Next, 54.dp) { car.next() }
            }
        }
    }
}

/** Playback position; it moves once a second, and only while something plays. */
@Composable
internal fun livePosition(m: NowPlaying): Long {
    val pos by produceState(m.positionMs, m) {
        while (true) {
            value = if (m.playing) m.positionMs + ((SystemClock.elapsedRealtime() - m.positionAt) * m.rate).toLong() else m.positionMs
            if (!m.playing) break
            delay(1000)
        }
    }
    return pos
}

internal fun clock(ms: Long): String { val s = (ms / 1000).coerceAtLeast(0); return "%d:%02d".format(s / 60, s % 60) }

@Composable
private fun Cover(art: ImageBitmap?, modifier: Modifier) {
    Box(modifier.clip(RoundedCornerShape(16.dp)).background(CardHi), contentAlignment = Alignment.Center) {
        if (art != null) Image(art, null, Modifier.fillMaxSize(), contentScale = ContentScale.Crop) else Glyph(G.Note, 30.dp, TextLo)
    }
}

@Composable
internal fun RoundButton(g: G, size: Dp, accent: Boolean = false, onTap: () -> Unit) {
    PressCard(Modifier.size(size), radius = size / 2, color = if (accent) SkyDeep else CardHi, onTap = onTap) {
        Glyph(g, size * 0.4f, TextHi, Modifier.align(Alignment.Center))
    }
}

/** App icon rendered off the main thread once, then served from cache. */
@Composable
fun AppIcon(ctx: Context, pkg: String, size: Dp) {
    val img by produceState(Apps.cached(pkg), pkg) { if (value == null) value = withContext(Dispatchers.Default) { Apps.icon(ctx, pkg) } }
    Box(Modifier.size(size)) { img?.let { Image(it, null, Modifier.fillMaxSize()) } }
}
