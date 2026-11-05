package com.local.carhome

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.drawWithCache
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.CompositingStrategy
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.drawscope.rotate
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import kotlin.math.roundToInt
import kotlin.random.Random

private val MapBase = Color(0xFF232427)
private val Street = Color(0xFF34353A)
private val Glass = Color(0xE62A2B2F)

/**
 * The navigation card: a calm, drawn street backdrop (generated once per size, so it costs nothing per frame)
 * with the Navigate action and live heading / GPS quality. Tap opens the nav app, long-press chooses it.
 */
@Composable
fun MapCard(car: CarState, navLabel: String?, navPkg: String?, modifier: Modifier, onTap: () -> Unit, onLong: () -> Unit) {
    val ctx = LocalContext.current
    PressCard(modifier, color = MapBase, onTap = onTap, onLong = onLong) {
        // A cached layer: it repaints with each GPS fix, not each time something else on the dashboard moves.
        Box(Modifier.fillMaxSize().graphicsLayer { compositingStrategy = CompositingStrategy.Offscreen }.drawWithCache {
            val rnd = Random(7)
            val w = size.width; val h = size.height
            val streets = Path(); val minor = Path()
            var x = -40f                                         // gently skewed avenues and cross streets, like a city grid
            while (x < w + 40f) { val sk = rnd.nextFloat() * 90f - 45f; (if (rnd.nextFloat() < 0.35f) streets else minor).apply { moveTo(x, -10f); lineTo(x + sk, h + 10f) }; x += 70f + rnd.nextFloat() * 90f }
            var y = -30f
            while (y < h + 30f) { val sk = rnd.nextFloat() * 60f - 30f; (if (rnd.nextFloat() < 0.35f) streets else minor).apply { moveTo(-10f, y); lineTo(w + 10f, y + sk) }; y += 60f + rnd.nextFloat() * 80f }
            val route = Path().apply {
                moveTo(w * 0.26f, h * 0.50f); lineTo(w * 0.44f, h * 0.24f); lineTo(w * 0.62f, h * 0.44f); lineTo(w * 0.80f, h * 0.20f); lineTo(w * 0.88f, h * 0.10f)
            }
            val thin = Stroke(1.2f.dp.toPx()); val thick = Stroke(3.2f.dp.toPx())
            val routeGlow = Stroke(12.dp.toPx(), cap = StrokeCap.Round, join = StrokeJoin.Round)
            val routeLine = Stroke(4.dp.toPx(), cap = StrokeCap.Round, join = StrokeJoin.Round)
            val me = Offset(w * 0.26f, h * 0.50f)
            val u = 1.dp.toPx()
            val arrow = Path().apply {                           // car marker arrow, pointing up before rotation
                moveTo(me.x, me.y - 11 * u); lineTo(me.x + 8 * u, me.y + 9 * u); lineTo(me.x, me.y + 5 * u); lineTo(me.x - 8 * u, me.y + 9 * u); close()
            }
            onDrawBehind {
                drawPath(minor, Street, style = thin)
                drawPath(streets, Street, style = thick)
                drawPath(route, Sky.copy(alpha = 0.15f), style = routeGlow)
                drawPath(route, Sky, style = routeLine)
                drawCircle(Sky.copy(alpha = 0.20f), 30 * u, me)
                drawCircle(CardHi, 21 * u, me)
                rotate(car.fix?.bearing ?: 0f, me) { drawPath(arrow, TextHi) }   // reading the fix here only redraws, never recomposes
            }
        })
        if (navPkg != null) Box(Modifier.align(Alignment.TopEnd).padding(18.dp).size(58.dp).clip(CircleShape).background(Glass), contentAlignment = Alignment.Center) {
            AppIcon(ctx, navPkg, 34.dp)
        }
        Row(Modifier.align(Alignment.BottomStart).fillMaxWidth().padding(18.dp), horizontalArrangement = Arrangement.spacedBy(14.dp)) {
            Row(Modifier.fillMaxWidth(0.58f).height(96.dp).clip(RoundedCornerShape(18.dp)).background(Glass).padding(horizontal = 20.dp),
                verticalAlignment = Alignment.CenterVertically) {
                Glyph(G.Nav, 30.dp, Sky)
                Spacer(Modifier.width(18.dp))
                Column {
                    Txt("Navigate", t(30f, TextHi, FontWeight.SemiBold, ls = -0.5f))
                    Txt(navLabel?.let { "Open $it" } ?: "Choose a maps app", t(15f, TextMid), maxLines = 1, overflow = TextOverflow.Ellipsis)
                }
            }
        }
    }
}

internal fun compass(deg: Float) = listOf("N", "NE", "E", "SE", "S", "SW", "W", "NW")[(((deg % 360) + 360 + 22.5f) / 45f).toInt() % 8]
