package com.local.carhome

import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.slideInHorizontally
import androidx.compose.animation.slideOutHorizontally
import androidx.compose.animation.togetherWith
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.gestures.detectVerticalDragGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
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
import androidx.compose.foundation.lazy.LazyListState
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.launch

private const val BT_PKG = "com.android.bluetooth"
private val NowRow = Color(0x1F7DBDF2)

/**
 * The Music screen beside the rail, so the phone never needs a touch while driving: what plays now on the left, the
 * phone's library on the right as iPod-style lists. One tap opens a folder or plays a track; albums and playlists also
 * get a play button to start them whole. A local music app, if one is installed, opens from the header.
 */
@Composable
fun MusicScreen(car: CarState, bt: BtLink, lib: PhoneLibrary, app: String?, appLabel: String?, modifier: Modifier, onPairPhone: () -> Unit) {
    val phone = bt.phones.firstOrNull { it.connected }
    DisposableEffect(Unit) { lib.setShown(true); onDispose { lib.setShown(false) } }
    LaunchedEffect(phone?.address) { lib.forPhone(phone?.address) }
    Row(modifier, horizontalArrangement = Arrangement.spacedBy(16.dp)) {
        NowPlayingPanel(car, lib, phone, Modifier.width(420.dp).fillMaxHeight())
        LibraryPanel(lib, bt, phone, car.media?.title, app, appLabel, Modifier.weight(1f).fillMaxHeight(), onPairPhone)
    }
}

// ------------------------------------------------------------------ now playing

@Composable
private fun NowPlayingPanel(car: CarState, lib: PhoneLibrary, phone: BtDev?, modifier: Modifier) {
    val m = car.media
    Column(modifier.clip(PanelShape).background(Panel).padding(24.dp), horizontalAlignment = Alignment.CenterHorizontally) {
        Txt("NOW PLAYING", t(13f, TextLo, FontWeight.SemiBold, ls = 1.5f), Modifier.fillMaxWidth())
        Spacer(Modifier.height(16.dp))
        Box(Modifier.size(260.dp).clip(RoundedCornerShape(24.dp)).background(CardHi), contentAlignment = Alignment.Center) {
            val art = m?.art
            if (art != null) Image(art, null, Modifier.fillMaxSize(), contentScale = ContentScale.Crop) else Glyph(G.Note, 72.dp, TextLo)
        }
        Spacer(Modifier.height(22.dp))
        Txt(m?.title?.ifBlank { null } ?: "Nothing playing", t(26f, TextHi, FontWeight.SemiBold, center = true), Modifier.fillMaxWidth(),
            maxLines = 2, overflow = TextOverflow.Ellipsis)
        Spacer(Modifier.height(4.dp))
        Txt(m?.artist?.ifBlank { null } ?: if (m == null && phone != null) "Pick something on the right" else "", t(18f, TextMid, center = true),
            Modifier.fillMaxWidth(), maxLines = 1, overflow = TextOverflow.Ellipsis)
        val source = when {
            m == null -> ""
            m.pkg == BT_PKG -> "From " + (phone?.name ?: "your phone")
            else -> m.app
        }
        Txt(source, t(14f, TextLo, center = true), Modifier.fillMaxWidth().padding(top = 6.dp), maxLines = 1, overflow = TextOverflow.Ellipsis)
        Spacer(Modifier.weight(1f))
        if (m != null && m.durationMs > 0) Progress(m)
        Spacer(Modifier.height(20.dp))
        // With nothing active yet, the buttons go straight to the phone (Play resumes whatever it played last).
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceEvenly, verticalAlignment = Alignment.CenterVertically) {
            RoundButton(G.Prev, 72.dp) { if (m != null) car.prev() else lib.prev() }
            RoundButton(if (m?.playing == true) G.Pause else G.Play, 96.dp, accent = true) { if (m != null) car.playPause() else lib.play() }
            RoundButton(G.Next, 72.dp) { if (m != null) car.next() else lib.next() }
        }
    }
}

/** Elapsed on the left, time left on the right, like an iPod. */
@Composable
private fun Progress(m: NowPlaying) {
    val pos = livePosition(m)
    Column(Modifier.fillMaxWidth()) {
        Box(Modifier.fillMaxWidth().height(4.dp).clip(RoundedCornerShape(2.dp)).background(Track)) {
            Box(Modifier.fillMaxWidth((pos.toFloat() / m.durationMs).coerceIn(0f, 1f)).fillMaxHeight().background(Sky))
        }
        Spacer(Modifier.height(8.dp))
        Row(Modifier.fillMaxWidth()) {
            Txt(clock(pos), t(13f, TextMid, tabular = true), Modifier.weight(1f))
            Txt("-" + clock(m.durationMs - pos), t(13f, TextMid, tabular = true))
        }
    }
}

// ------------------------------------------------------------------ library

@Composable
private fun LibraryPanel(lib: PhoneLibrary, bt: BtLink, phone: BtDev?, nowTitle: String?, app: String?, appLabel: String?, modifier: Modifier, onPairPhone: () -> Unit) {
    val ctx = LocalContext.current
    val crumbs = lib.path.filter { !it.auto }
    Column(modifier.clip(PanelShape).background(Panel).padding(start = 24.dp, top = 22.dp, end = 16.dp)) {
        Row(Modifier.fillMaxWidth().height(72.dp).padding(end = 8.dp), verticalAlignment = Alignment.CenterVertically) {
            if (phone != null && lib.canGoBack) {
                PressCard(Modifier.size(64.dp), radius = 32.dp, color = CardHi, onTap = { lib.back() }) { Glyph(G.Back, 26.dp, TextHi, Modifier.align(Alignment.Center)) }
                Spacer(Modifier.width(18.dp))
            }
            Column(Modifier.weight(1f)) {
                Txt(if (phone != null) crumbs.lastOrNull()?.title ?: "Music" else "Music", t(30f, TextHi, FontWeight.SemiBold, ls = -0.5f),
                    maxLines = 1, overflow = TextOverflow.Ellipsis)
                Txt(if (phone == null) "No phone connected" else (listOf(phone.name) + crumbs.dropLast(1).map { it.title }).joinToString("  \u203A  "),
                    t(15f, TextMid), maxLines = 1, overflow = TextOverflow.Ellipsis)
            }
            if (app != null) { Spacer(Modifier.width(12.dp)); Pill("Open " + (appLabel ?: "app")) { Apps.launch(ctx, app) } }
        }
        Spacer(Modifier.height(14.dp))
        when {
            phone == null -> NoPhone(bt, onPairPhone)
            !lib.available -> Message("Bluetooth music is not available", "The tablet's Bluetooth browse service did not answer. The controls on the left still work.")
            // Slides like an iPod: in from the right going deeper, back from the left going up. Levels entered on
            // their own (phone, single music app) keep the same depth, so they swap in place without a slide.
            else -> AnimatedContent(lib.path.lastOrNull()?.id to crumbs.size, Modifier.fillMaxSize(), contentKey = { it.second }, transitionSpec = {
                val deeper = targetState.second > initialState.second
                (slideInHorizontally(tween(260)) { w -> if (deeper) w / 6 else -w / 6 } + fadeIn(tween(220))) togetherWith
                    (slideOutHorizontally(tween(200)) { w -> if (deeper) -w / 6 else w / 6 } + fadeOut(tween(160)))
            }, label = "level") { (id, _) ->
                Level(lib, id, nowTitle, phone.name)
            }
        }
    }
}

@Composable
private fun Level(lib: PhoneLibrary, id: String?, nowTitle: String?, phoneName: String) {
    val items = id?.let { lib.levels[it] }
    when {
        items == null -> Waiting(if (lib.slow) "$phoneName is not answering yet" else "Loading from $phoneName")
        items.isEmpty() && lib.path.all { it.auto } -> Message("$phoneName does not share its library",
            "Some phones and music apps don't offer browsing over Bluetooth. The controls on the left still work.")
        items.isEmpty() -> Message("Nothing in here", "")
        else -> ItemList(lib, items, nowTitle)
    }
}

@Composable
private fun ItemList(lib: PhoneLibrary, items: List<LibItem>, nowTitle: String?) {
    val state = rememberLazyListState()
    Row(Modifier.fillMaxSize()) {
        LazyColumn(Modifier.weight(1f).fillMaxHeight(), state = state, contentPadding = PaddingValues(bottom = 20.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            items(items) { item ->
                ItemRow(item, !item.folder && item.title == nowTitle, onOpen = { if (item.folder) lib.open(item) else lib.play(item) }, onPlay = { lib.play(item) })
            }
        }
        if (items.size > 30) LetterRail(items, state)
    }
}

@Composable
private fun ItemRow(item: LibItem, current: Boolean, onOpen: () -> Unit, onPlay: () -> Unit) {
    PressCard(Modifier.fillMaxWidth().height(84.dp), radius = 20.dp, color = if (current) NowRow else Card, onTap = onOpen) {
        Row(Modifier.fillMaxSize().padding(start = 20.dp, end = 14.dp), verticalAlignment = Alignment.CenterVertically) {
            if (!item.folder) {
                Box(Modifier.size(48.dp).clip(RoundedCornerShape(12.dp)).background(CardHi), contentAlignment = Alignment.Center) {
                    Glyph(if (current) G.Speaker else G.Note, 22.dp, if (current) Sky else TextLo)
                }
                Spacer(Modifier.width(16.dp))
            }
            Column(Modifier.weight(1f)) {
                Txt(item.title.ifBlank { "Untitled" }, t(21f, if (current) Sky else TextHi, FontWeight.Medium), maxLines = 1, overflow = TextOverflow.Ellipsis)
                if (item.subtitle.isNotBlank()) Txt(item.subtitle, t(15f, TextMid), maxLines = 1, overflow = TextOverflow.Ellipsis)
            }
            if (item.folder && item.playable) {   // an album or playlist: start it whole without opening it
                PressCard(Modifier.size(52.dp), radius = 26.dp, color = CardHi, onTap = onPlay) { Glyph(G.Play, 20.dp, TextHi, Modifier.align(Alignment.Center)) }
                Spacer(Modifier.width(12.dp))
            }
            if (item.folder) Glyph(G.Chevron, 22.dp, TextLo)
        }
    }
}

/** A-Z strip beside long lists (artists, songs): tap a letter or slide a finger along it to jump. */
@Composable
private fun LetterRail(items: List<LibItem>, state: LazyListState) {
    val first = remember(items) { LinkedHashMap<Char, Int>().also { m -> items.forEachIndexed { i, it -> m.getOrPut(initial(it.title)) { i } } } }
    if (first.size < 4) return
    val letters = remember(first) { first.keys.toList() }
    val scope = rememberCoroutineScope()
    val last = remember { arrayOfNulls<Char>(1) }
    fun jump(y: Float, h: Int) {
        val c = letters[(y / h * letters.size).toInt().coerceIn(0, letters.lastIndex)]
        if (c != last[0]) { last[0] = c; scope.launch { state.scrollToItem(first.getValue(c)) } }
    }
    Column(Modifier.width(48.dp).fillMaxHeight().padding(vertical = 6.dp)
        .pointerInput(letters) { detectTapGestures { last[0] = null; jump(it.y, size.height) } }
        .pointerInput(letters) { detectVerticalDragGestures(onDragStart = { last[0] = null; jump(it.y, size.height) }) { ch, _ -> jump(ch.position.y, size.height) } },
        horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.SpaceEvenly) {
        for (c in letters) Txt(c.toString(), t(13f, TextMid, FontWeight.SemiBold))
    }
}

/** Index letter the way an iPod files names: "The Beatles" under B, digits and symbols under #. */
private fun initial(title: String): Char {
    val c = title.trim().removePrefix("The ").firstOrNull()?.uppercaseChar() ?: '#'
    return if (c in 'A'..'Z') c else '#'
}

// ------------------------------------------------------------------ states

@Composable
private fun NoPhone(bt: BtLink, onPairPhone: () -> Unit) {
    val paired = bt.phones.firstOrNull()
    Column(Modifier.fillMaxSize().padding(bottom = 40.dp), horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.Center) {
        Box(Modifier.size(84.dp).clip(CircleShape).background(Color(0x337DBDF2)), contentAlignment = Alignment.Center) { Glyph(G.Phone, 40.dp, Sky) }
        Spacer(Modifier.height(22.dp))
        Txt(if (paired != null) "Connect your phone" else "Pair your phone", t(30f, TextHi, FontWeight.SemiBold, center = true))
        Spacer(Modifier.height(10.dp))
        Txt("Its playlists, artists, albums and songs show up here, ready to play from the tablet.", t(18f, TextMid, center = true), Modifier.width(520.dp))
        Spacer(Modifier.height(24.dp))
        if (paired != null) Pill("Connect " + paired.name) { bt.connect(paired.address, Side.PHONE) } else Pill("Pair phone", onTap = onPairPhone)
    }
}

/** Waiting for the phone: one softly pulsing dot (drawn in its layer, so the pulse recomposes nothing). */
@Composable
private fun Waiting(text: String) {
    val pulse = rememberInfiniteTransition(label = "wait").animateFloat(0.25f, 1f, infiniteRepeatable(tween(800), RepeatMode.Reverse), label = "pulse")
    Box(Modifier.fillMaxSize().padding(bottom = 40.dp), contentAlignment = Alignment.Center) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Box(Modifier.size(12.dp).graphicsLayer { alpha = pulse.value }.clip(CircleShape).background(Sky))
            Spacer(Modifier.width(12.dp))
            Txt(text, t(19f, TextMid))
        }
    }
}

@Composable
private fun Message(title: String, body: String) {
    Column(Modifier.fillMaxSize().padding(bottom = 40.dp), horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.Center) {
        Txt(title, t(24f, TextHi, FontWeight.SemiBold, center = true))
        if (body.isNotEmpty()) { Spacer(Modifier.height(10.dp)); Txt(body, t(17f, TextMid, center = true), Modifier.width(520.dp)) }
    }
}
