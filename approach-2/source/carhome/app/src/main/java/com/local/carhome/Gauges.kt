package com.local.carhome

import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.size
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.text.drawText
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.rememberTextMeasurer
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import kotlin.math.cos
import kotlin.math.sin

private const val START = 135f   // gauges open at the bottom: 270 degrees of arc starting lower-left
private const val SWEEP = 270f

private fun DrawScope.arc(color: Color, from: Float, sweep: Float, inset: Float, width: Float, brush: Brush? = null) {
    val tl = Offset(inset, inset); val sz = Size(size.width - 2 * inset, size.height - 2 * inset)
    val st = Stroke(width, cap = StrokeCap.Round)
    if (brush != null) drawArc(brush, from, sweep, false, tl, sz, style = st) else drawArc(color, from, sweep, false, tl, sz, style = st)
}

/**
 * Speedometer: tick ring with labels, a sky-blue progress arc with a soft glow, and the speed in the middle.
 * [speed] is in the display unit; null shows a dash (no GPS fix).
 */
@Composable
fun Speedometer(speed: Int?, max: Int, unit: String, modifier: Modifier) {
    val measurer = rememberTextMeasurer()
    val labelStyle = remember { t(15f, TextMid, FontWeight.Medium) }
    val labels = remember(max) { (0..max step 20).map { it to measurer.measure(it.toString(), labelStyle) } }
    val shown by animateFloatAsState((speed ?: 0).coerceIn(0, max).toFloat(), tween(450), label = "speed")
    Box(modifier.aspectRatio(1f), contentAlignment = Alignment.Center) {
        Canvas(Modifier.fillMaxSize()) {
            val r = size.minDimension / 2; val c = Offset(size.width / 2, size.height / 2)
            arc(Track, START, SWEEP, 10.dp.toPx(), 3.dp.toPx())
            val frac = shown / max
            if (frac > 0.003f) {
                arc(Sky.copy(alpha = 0.16f), START, SWEEP * frac, 10.dp.toPx(), 16.dp.toPx())                  // glow
                arc(Sky, START, SWEEP * frac, 10.dp.toPx(), 5.dp.toPx(), Brush.sweepGradient(listOf(SkyDeep, Sky, SkyDeep), c))
            }
            for (v in 0..max step 5) {                                                                // ticks: long every 20
                val a = Math.toRadians((START + SWEEP * v / max).toDouble())
                val major = v % 20 == 0
                val r1 = r - 26.dp.toPx(); val r2 = r1 - (if (major) 14.dp else 7.dp).toPx()
                drawLine(if (v <= shown) TextHi else TextLo, Offset(c.x + r1 * cos(a).toFloat(), c.y + r1 * sin(a).toFloat()),
                    Offset(c.x + r2 * cos(a).toFloat(), c.y + r2 * sin(a).toFloat()), (if (major) 2.2f else 1.3f).dp.toPx(), StrokeCap.Round)
            }
            val rl = r - 60.dp.toPx()                                                                 // labels inside the ticks
            for ((v, layout) in labels) {
                val a = Math.toRadians((START + SWEEP * v / max).toDouble())
                drawText(layout, topLeft = Offset(c.x + rl * cos(a).toFloat() - layout.size.width / 2f, c.y + rl * sin(a).toFloat() - layout.size.height / 2f))
            }
            drawCircle(Brush.radialGradient(listOf(CardHi, Card), c, r * 0.36f), r * 0.34f, c)            // hub
        }
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Txt(speed?.toString() ?: "–", t(64f, if (speed != null) TextHi else TextLo, FontWeight.SemiBold, ls = -2f, tabular = true))
            Txt(unit, t(16f, TextMid, FontWeight.Medium))
        }
    }
}

/** Small ring gauge (battery, volume, GPS): icon, value and label inside a 270 degree arc. */
@Composable
fun RingGauge(frac: Float, color: Color, icon: G, value: String, label: String, size: Dp = 128.dp) {
    val shown by animateFloatAsState(frac.coerceIn(0f, 1f), tween(400), label = "ring")
    Box(Modifier.size(size), contentAlignment = Alignment.Center) {
        Canvas(Modifier.fillMaxSize()) {
            arc(Track, START, SWEEP, 4.dp.toPx(), 3.dp.toPx())
            if (shown > 0.003f) arc(color, START, SWEEP * shown, 4.dp.toPx(), 3.5f.dp.toPx())
        }
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Glyph(icon, 16.dp, color)
            Spacer(Modifier.height(4.dp))
            Txt(value, t(22f, color, FontWeight.SemiBold, tabular = true))
            Txt(label, t(13f, TextMid))
        }
    }
}
