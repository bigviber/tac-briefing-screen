package com.local.carhome

import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp

/** Every app, in a calm grid with big targets. */
@Composable
fun Drawer(apps: List<AppEntry>, onClose: () -> Unit, onOpen: (String) -> Unit) {
    val ctx = LocalContext.current
    Column(Modifier.fillMaxSize().background(Color(0xF5161719)).pointerInput(Unit) { detectTapGestures { } }.padding(horizontal = 36.dp, vertical = 28.dp)) {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Txt("All apps", t(40f, TextHi, FontWeight.SemiBold, ls = -1f))
            }
            CloseButton(onClose)
        }
        Spacer(Modifier.height(24.dp))
        LazyVerticalGrid(GridCells.Adaptive(150.dp), Modifier.fillMaxSize(), contentPadding = PaddingValues(bottom = 24.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp), horizontalArrangement = Arrangement.spacedBy(16.dp)) {
            items(apps, key = { it.pkg }) { a ->
                PressCard(Modifier.fillMaxWidth().height(150.dp), radius = 24.dp, color = Card, onTap = { onOpen(a.pkg) }) {
                    Column(Modifier.align(Alignment.Center), horizontalAlignment = Alignment.CenterHorizontally) {
                        AppIcon(ctx, a.pkg, 64.dp)
                        Spacer(Modifier.height(12.dp))
                        Txt(a.label, t(16f, TextHi, FontWeight.Medium, center = true), Modifier.padding(horizontal = 12.dp), maxLines = 1, overflow = TextOverflow.Ellipsis)
                    }
                }
            }
        }
    }
}

/** Chooses which app the big Navigate card opens. */
@Composable
fun NavPicker(options: List<String>, label: (String) -> String, current: String?, onPick: (String) -> Unit, onClose: () -> Unit) {
    val ctx = LocalContext.current
    Box(Modifier.fillMaxSize().background(Color(0xE6161719)).pointerInput(Unit) { detectTapGestures { onClose() } }, contentAlignment = Alignment.Center) {
        Column(Modifier.width(620.dp).clip(RoundedCornerShape(28.dp)).background(Card).pointerInput(Unit) { detectTapGestures { } }.padding(28.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Txt("Navigation app", t(30f, TextHi, FontWeight.SemiBold), Modifier.weight(1f))
                CloseButton(onClose)
            }
            if (options.isEmpty()) Txt("No maps app is installed yet.", t(19f, TextMid))
            for (p in options) PressCard(Modifier.fillMaxWidth().height(84.dp), radius = 20.dp, onTap = { onPick(p) }) {
                Row(Modifier.fillMaxSize().padding(horizontal = 20.dp), verticalAlignment = Alignment.CenterVertically) {
                    AppIcon(ctx, p, 48.dp)
                    Spacer(Modifier.width(18.dp))
                    Txt(label(p), t(22f, TextHi, FontWeight.Medium), Modifier.weight(1f))
                    if (p == current) Box(Modifier.size(14.dp).clip(CircleShape).background(Sky))
                }
            }
        }
    }
}

@Composable
private fun CloseButton(onClose: () -> Unit) {
    PressCard(Modifier.size(72.dp), radius = 36.dp, color = CardHi, onTap = onClose) { Glyph(G.Close, 28.dp, TextHi, Modifier.align(Alignment.Center)) }
}
