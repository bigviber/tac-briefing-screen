package com.local.carhome

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.size
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import kotlin.math.cos
import kotlin.math.sin

/** Icons drawn on a 24-unit grid: sharp at any size, no image assets. */
enum class G { Home, Grid, Nav, Note, Gear, Play, Pause, Prev, Next, Lock, Unlock, Bluetooth, Wifi, Gps, Speaker, Bolt, Close, Phone, Car, Arrow, Chevron, Back, Call, Mic, MicOff, Bell, Alarm }

@Composable
fun Glyph(g: G, size: Dp, color: Color = TextHi, modifier: Modifier = Modifier) {
    Canvas(modifier.size(size)) {
        val u = this.size.minDimension / 24f
        val line = Stroke(width = 1.9f * u, cap = StrokeCap.Round, join = StrokeJoin.Round)
        fun o(x: Float, y: Float) = Offset(x * u, y * u)
        fun p(vararg pts: Float, close: Boolean = true) = Path().apply {
            moveTo(pts[0] * u, pts[1] * u)
            var i = 2
            while (i < pts.size) { lineTo(pts[i] * u, pts[i + 1] * u); i += 2 }
            if (close) close()
        }
        fun bar(x: Float) = drawRoundRect(color, o(x, 5f), Size(2.6f * u, 14f * u), CornerRadius(1f * u))
        when (g) {
            G.Home -> drawPath(p(3.5f, 11f, 12f, 3.5f, 20.5f, 11f, 20.5f, 20.5f, 14.5f, 20.5f, 14.5f, 14.5f, 9.5f, 14.5f, 9.5f, 20.5f, 3.5f, 20.5f), color)
            G.Grid -> for (r in 0..1) for (c in 0..1) drawRoundRect(color, o(4f + c * 9f, 4f + r * 9f), Size(7f * u, 7f * u), CornerRadius(2f * u))
            G.Nav -> drawPath(p(12f, 2.5f, 20f, 21f, 12f, 16.5f, 4f, 21f), color)
            G.Note -> {
                drawPath(p(9f, 17.5f, 9f, 5f, 19f, 3f, 19f, 15.5f, close = false), color, style = line)
                drawCircle(color, 2.8f * u, o(6.4f, 17.6f)); drawCircle(color, 2.8f * u, o(16.4f, 15.6f))
            }
            G.Gear -> {
                drawCircle(color, 6.2f * u, o(12f, 12f), style = Stroke(3.2f * u))
                for (k in 0 until 8) {
                    val a = Math.toRadians(k * 45.0)
                    drawLine(color, o(12f + 7.2f * cos(a).toFloat(), 12f + 7.2f * sin(a).toFloat()), o(12f + 10f * cos(a).toFloat(), 12f + 10f * sin(a).toFloat()), 3f * u, StrokeCap.Round)
                }
            }
            G.Play -> drawPath(p(7f, 4.5f, 19.5f, 12f, 7f, 19.5f), color)
            G.Pause -> { drawRoundRect(color, o(6f, 4.5f), Size(4f * u, 15f * u), CornerRadius(1.2f * u)); drawRoundRect(color, o(14f, 4.5f), Size(4f * u, 15f * u), CornerRadius(1.2f * u)) }
            G.Prev -> { drawPath(p(19f, 5f, 9f, 12f, 19f, 19f), color); bar(5f) }
            G.Next -> { drawPath(p(5f, 5f, 15f, 12f, 5f, 19f), color); bar(16.4f) }
            G.Lock, G.Unlock -> {
                drawRoundRect(color, o(5f, 10.5f), Size(14f * u, 10f * u), CornerRadius(2.5f * u))
                drawPath(Path().apply {
                    moveTo(8f * u, 10.5f * u); lineTo(8f * u, 7.5f * u)
                    cubicTo(8f * u, 3f * u, 16f * u, 3f * u, 16f * u, 7.5f * u)
                    if (g == G.Lock) lineTo(16f * u, 10.5f * u)
                }, color, style = line)
            }
            G.Bluetooth -> drawPath(p(7f, 7.5f, 17f, 16f, 12f, 20.5f, 12f, 3.5f, 17f, 8f, 7f, 16.5f, close = false), color, style = line)
            G.Wifi -> {
                for (r in listOf(9.5f, 6.2f)) drawArc(color, 225f, 90f, false, o(12f - r * 1.55f, 18.5f - r * 1.55f), Size(r * 3.1f * u, r * 3.1f * u), style = line)
                drawCircle(color, 1.8f * u, o(12f, 18.5f))
            }
            G.Gps -> {
                drawCircle(color, 7.5f * u, o(12f, 12f), style = line)
                drawCircle(color, 2.6f * u, o(12f, 12f))
                for ((a, b) in listOf(o(12f, 1.5f) to o(12f, 4.5f), o(12f, 19.5f) to o(12f, 22.5f), o(1.5f, 12f) to o(4.5f, 12f), o(19.5f, 12f) to o(22.5f, 12f)))
                    drawLine(color, a, b, line.width, StrokeCap.Round)
            }
            G.Speaker -> {
                drawPath(p(3.5f, 9f, 7.5f, 9f, 12.5f, 4.5f, 12.5f, 19.5f, 7.5f, 15f, 3.5f, 15f), color)
                drawArc(color, -50f, 100f, false, o(10.5f, 7f), Size(10f * u, 10f * u), style = line)
            }
            G.Bolt -> drawPath(p(13.5f, 2f, 5f, 13.5f, 11f, 13.5f, 10f, 22f, 19f, 10f, 13f, 10f), color)
            G.Phone -> {
                drawRoundRect(color, o(6.5f, 2.5f), Size(11f * u, 19f * u), CornerRadius(2.6f * u), style = line)
                drawLine(color, o(10.5f, 18.5f), o(13.5f, 18.5f), line.width, StrokeCap.Round)
            }
            G.Car -> {
                drawPath(p(2.5f, 15.5f, 3.5f, 11f, 6.5f, 6.5f, 17.5f, 6.5f, 20.5f, 11f, 21.5f, 15.5f, 21.5f, 18f, 2.5f, 18f), color, style = line)
                drawLine(color, o(4f, 11f), o(20f, 11f), line.width, StrokeCap.Round)
                drawCircle(color, 1.6f * u, o(7f, 14.5f)); drawCircle(color, 1.6f * u, o(17f, 14.5f))
            }
            G.Arrow -> {
                drawLine(color, o(3f, 12f), o(20f, 12f), line.width, StrokeCap.Round)
                drawPath(p(14f, 6.5f, 20f, 12f, 14f, 17.5f, close = false), color, style = line)
            }
            G.Chevron -> drawPath(p(9f, 5f, 16f, 12f, 9f, 19f, close = false), color, style = line)
            G.Back -> drawPath(p(15f, 5f, 8f, 12f, 15f, 19f, close = false), color, style = line)
            G.Call -> {   // a handset lying flat: answer in mint, end in coral
                drawPath(Path().apply { moveTo(5f * u, 14.5f * u); cubicTo(6.5f * u, 7f * u, 17.5f * u, 7f * u, 19f * u, 14.5f * u) }, color,
                    style = Stroke(3.2f * u, cap = StrokeCap.Round))
                drawRoundRect(color, o(2.5f, 13f), Size(6.5f * u, 4.8f * u), CornerRadius(2f * u))
                drawRoundRect(color, o(15f, 13f), Size(6.5f * u, 4.8f * u), CornerRadius(2f * u))
            }
            G.Mic, G.MicOff -> {
                drawRoundRect(color, o(9f, 3f), Size(6f * u, 11f * u), CornerRadius(3f * u))
                drawArc(color, 0f, 180f, false, o(5.5f, 6.5f), Size(13f * u, 11f * u), style = line)
                drawLine(color, o(12f, 17.5f), o(12f, 21f), line.width, StrokeCap.Round)
                if (g == G.MicOff) drawLine(color, o(4f, 3.5f), o(20f, 20.5f), line.width, StrokeCap.Round)
            }
            G.Bell -> {
                drawPath(Path().apply {
                    moveTo(5f * u, 17f * u); lineTo(19f * u, 17f * u); lineTo(17.5f * u, 15f * u); lineTo(17.5f * u, 10.5f * u)
                    cubicTo(17.5f * u, 3.5f * u, 6.5f * u, 3.5f * u, 6.5f * u, 10.5f * u); lineTo(6.5f * u, 15f * u); close()
                }, color)
                drawCircle(color, 2f * u, o(12f, 19.5f))
            }
            G.Alarm -> {
                drawCircle(color, 7.5f * u, o(12f, 13f), style = line)
                drawLine(color, o(12f, 13f), o(12f, 9f), line.width, StrokeCap.Round)
                drawLine(color, o(12f, 13f), o(15f, 14.5f), line.width, StrokeCap.Round)
                drawLine(color, o(4f, 5.5f), o(7f, 3f), line.width, StrokeCap.Round)
                drawLine(color, o(20f, 5.5f), o(17f, 3f), line.width, StrokeCap.Round)
            }
            G.Close -> { drawLine(color, o(6f, 6f), o(18f, 18f), line.width, StrokeCap.Round); drawLine(color, o(18f, 6f), o(6f, 18f), line.width, StrokeCap.Round) }
        }
    }
}

/** Battery outline with a fill proportional to [level] (0..1): mint while charging, amber when low. */
@Composable
fun BatteryGlyph(level: Float, charging: Boolean, size: Dp = 26.dp) {
    Canvas(Modifier.size(size, size * 0.55f)) {
        val w = this.size.width; val h = this.size.height; val s = h / 12f
        drawRoundRect(TextMid, Offset.Zero, Size(w - 3f * s, h), CornerRadius(3f * s), style = Stroke(1.4f * s))
        drawRoundRect(TextMid, Offset(w - 2.2f * s, h * 0.32f), Size(2.2f * s, h * 0.36f), CornerRadius(1f * s))
        val c = when { charging -> Mint; level < 0.2f -> Amber; else -> TextHi }
        drawRoundRect(c, Offset(2.5f * s, 2.5f * s), Size((w - 8f * s) * level.coerceIn(0f, 1f), h - 5f * s), CornerRadius(1.6f * s))
    }
}
