package com.local.carhome

import androidx.compose.animation.core.Spring
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.spring
import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxScope
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicText
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.Font
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

// Graphite dashboard palette: soft charcoal layers (page < panel < card), sky-blue and mint arcs, no pure black or white.
internal val Page = Color(0xFF161719)
internal val Panel = Color(0xFF1F2023)
internal val Card = Color(0xFF2A2B2F)
internal val CardHi = Color(0xFF34353A)
internal val Track = Color(0xFF3A3B40)
internal val TextHi = Color(0xFFF2F2F4)
internal val TextMid = Color(0xFFA6A7AD)
internal val TextLo = Color(0xFF6F7076)
internal val Sky = Color(0xFF7DBDF2)
internal val SkyDeep = Color(0xFF3F8EE6)
internal val Mint = Color(0xFF86E3A6)
internal val Amber = Color(0xFFFFB45C)

internal val Inter = FontFamily(
    Font(R.font.inter_light, FontWeight.Light), Font(R.font.inter_regular, FontWeight.Normal),
    Font(R.font.inter_medium, FontWeight.Medium), Font(R.font.inter_semibold, FontWeight.SemiBold),
)

/** Text style shorthand. Tabular figures keep clock and speed digits from jittering as they change. */
internal fun t(size: Float, c: Color = TextHi, w: FontWeight = FontWeight.Normal, ls: Float = 0f, center: Boolean = false, tabular: Boolean = false) =
    TextStyle(
        color = c, fontSize = size.sp, fontFamily = Inter, fontWeight = w, letterSpacing = ls.sp,
        textAlign = if (center) TextAlign.Center else TextAlign.Start,
        fontFeatureSettings = if (tabular) "tnum" else null,
    )

/** Text with the style as the second argument (BasicText takes the modifier there). */
@Composable
internal fun Txt(text: String, style: TextStyle, modifier: Modifier = Modifier, maxLines: Int = Int.MAX_VALUE, overflow: TextOverflow = TextOverflow.Clip) =
    BasicText(text, modifier, style, overflow = overflow, maxLines = maxLines)

/**
 * A touch surface sized for arm's length: a soft spring press (slight scale and lift) with no flash.
 * The pressed scale is read inside graphicsLayer, so pressing redraws one layer and recomposes nothing.
 */
@Composable
internal fun PressCard(
    modifier: Modifier = Modifier,
    radius: Dp = 28.dp,
    color: Color = Card,
    onTap: (() -> Unit)? = null,
    onLong: (() -> Unit)? = null,
    content: @Composable BoxScope.() -> Unit,
) {
    var pressed by remember { mutableStateOf(false) }
    val scale by animateFloatAsState(if (pressed) 0.965f else 1f, spring(dampingRatio = 0.7f, stiffness = Spring.StiffnessMediumLow), label = "press")
    Box(
        modifier
            .graphicsLayer { scaleX = scale; scaleY = scale }
            .clip(RoundedCornerShape(radius))
            .background(color)
            .then(
                if (onTap == null && onLong == null) Modifier else Modifier.pointerInput(onTap, onLong) {
                    detectTapGestures(
                        onPress = { pressed = true; tryAwaitRelease(); pressed = false },
                        onTap = { onTap?.invoke() },
                        onLongPress = onLong?.let { l -> { _ -> l() } },
                    )
                }
            ),
        content = content,
    )
}
