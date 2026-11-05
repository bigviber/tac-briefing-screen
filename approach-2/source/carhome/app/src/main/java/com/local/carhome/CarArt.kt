package com.local.carhome

import androidx.compose.foundation.layout.Box
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.drawWithCache
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.CompositingStrategy
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.graphicsLayer

/**
 * A generic compact sedan seen from the front (no brand marks), drawn as vectors with glowing headlights on a
 * lit platform. All paths are built once per size in drawWithCache; drawing it costs a handful of path fills.
 */
@Composable
fun CarArt(modifier: Modifier) {
    // A cached layer: the gradients are not repainted each time something else on the dashboard moves.
    Box(modifier.graphicsLayer { compositingStrategy = CompositingStrategy.Offscreen }.drawWithCache {
        val w = size.width; val h = size.height
        fun x(f: Float) = w * f
        fun y(f: Float) = h * f
        fun poly(vararg p: Float) = Path().apply { moveTo(x(p[0]), y(p[1])); var i = 2; while (i < p.size) { lineTo(x(p[i]), y(p[i + 1])); i += 2 }; close() }

        val body = Path().apply {
            moveTo(x(0.08f), y(0.80f))
            lineTo(x(0.07f), y(0.60f)); quadraticBezierTo(x(0.08f), y(0.47f), x(0.16f), y(0.44f))
            lineTo(x(0.84f), y(0.44f)); quadraticBezierTo(x(0.92f), y(0.47f), x(0.93f), y(0.60f))
            lineTo(x(0.92f), y(0.80f)); quadraticBezierTo(x(0.90f), y(0.86f), x(0.84f), y(0.86f))
            lineTo(x(0.16f), y(0.86f)); quadraticBezierTo(x(0.10f), y(0.86f), x(0.08f), y(0.80f)); close()
        }
        val cabin = Path().apply {
            moveTo(x(0.20f), y(0.45f)); lineTo(x(0.29f), y(0.08f)); quadraticBezierTo(x(0.50f), y(0.02f), x(0.71f), y(0.08f))
            lineTo(x(0.79f), y(0.45f)); close()
        }
        val glass = Path().apply {
            moveTo(x(0.25f), y(0.43f)); lineTo(x(0.32f), y(0.12f)); quadraticBezierTo(x(0.50f), y(0.07f), x(0.68f), y(0.12f))
            lineTo(x(0.745f), y(0.43f)); close()
        }
        val lampL = poly(0.10f, 0.53f, 0.31f, 0.555f, 0.30f, 0.60f, 0.12f, 0.59f)
        val lampR = poly(0.90f, 0.53f, 0.69f, 0.555f, 0.70f, 0.60f, 0.88f, 0.59f)
        val bodyFill = Brush.verticalGradient(listOf(Color(0xFF3A3C43), Color(0xFF202125), Color(0xFF141518)), y(0.04f), y(0.86f))
        val glassFill = Brush.verticalGradient(listOf(Color(0xFF2B3442), Color(0xFF0E1116)), y(0.08f), y(0.43f))
        val glowL = Brush.radialGradient(listOf(Sky.copy(alpha = 0.55f), Color.Transparent), Offset(x(0.21f), y(0.57f)), w * 0.17f)
        val glowR = Brush.radialGradient(listOf(Sky.copy(alpha = 0.55f), Color.Transparent), Offset(x(0.79f), y(0.57f)), w * 0.17f)
        val floor = Brush.radialGradient(listOf(Color(0xFF34363C), Color(0x00000000)), Offset(x(0.5f), y(0.90f)), w * 0.5f)
        val edge = Stroke(1.2f * density)
        onDrawBehind {
            drawOval(floor, Offset(x(0.0f), y(0.80f)), Size(w, y(0.20f)))                                         // lit platform
            drawOval(Color(0x33FFFFFF), Offset(x(0.04f), y(0.82f)), Size(x(0.92f), y(0.16f)), style = edge)
            drawRoundRect(Color(0xFF0D0E10), Offset(x(0.11f), y(0.78f)), Size(x(0.11f), y(0.13f)), CornerRadius(8f * density))   // tyres
            drawRoundRect(Color(0xFF0D0E10), Offset(x(0.78f), y(0.78f)), Size(x(0.11f), y(0.13f)), CornerRadius(8f * density))
            drawPath(cabin, bodyFill)
            drawPath(glass, glassFill)
            drawOval(Color(0xFF26272C), Offset(x(0.15f), y(0.40f)), Size(x(0.07f), y(0.06f)))                     // mirrors
            drawOval(Color(0xFF26272C), Offset(x(0.78f), y(0.40f)), Size(x(0.07f), y(0.06f)))
            drawPath(body, bodyFill)
            drawPath(body, Color(0x22FFFFFF), style = edge)
            drawRoundRect(Color(0xFF101114), Offset(x(0.35f), y(0.54f)), Size(x(0.30f), y(0.13f)), CornerRadius(10f * density))  // grille
            for (k in 1..3) drawLine(Color(0xFF2C2D32), Offset(x(0.37f), y(0.54f + k * 0.032f)), Offset(x(0.63f), y(0.54f + k * 0.032f)), 1.5f * density)
            drawRoundRect(Color(0xFF0E0F12), Offset(x(0.30f), y(0.72f)), Size(x(0.40f), y(0.06f)), CornerRadius(6f * density))  // lower intake
            drawRect(glowL, Offset(x(0.0f), y(0.40f)), Size(x(0.42f), y(0.36f)))
            drawRect(glowR, Offset(x(0.58f), y(0.40f)), Size(x(0.42f), y(0.36f)))
            drawPath(lampL, Color(0xFFDCEEFF)); drawPath(lampR, Color(0xFFDCEEFF))
            drawLine(Sky, Offset(x(0.15f), y(0.75f)), Offset(x(0.24f), y(0.75f)), 3f * density, StrokeCap.Round)        // daytime running lights
            drawLine(Sky, Offset(x(0.76f), y(0.75f)), Offset(x(0.85f), y(0.75f)), 3f * density, StrokeCap.Round)
        }
    })
}
