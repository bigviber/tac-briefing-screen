package com.carivi.relay.ui

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp

/** Always-dark automotive palette: high contrast, low glare, large type. */
private val CarColors = darkColorScheme(
    primary = Color(0xFF4FC3F7),
    onPrimary = Color(0xFF00262F),
    primaryContainer = Color(0xFF0E3A4A),
    onPrimaryContainer = Color(0xFFBFE9FF),
    secondary = Color(0xFF80CBC4),
    onSecondary = Color(0xFF00201C),
    secondaryContainer = Color(0xFF123C38),
    onSecondaryContainer = Color(0xFFB2DFDB),
    tertiary = Color(0xFFFFB74D),
    onTertiary = Color(0xFF2B1700),
    error = Color(0xFFFF8A80),
    onError = Color(0xFF3B0000),
    background = Color(0xFF070D14),
    onBackground = Color(0xFFE3EEF5),
    surface = Color(0xFF0C1620),
    onSurface = Color(0xFFE3EEF5),
    surfaceVariant = Color(0xFF15232F),
    onSurfaceVariant = Color(0xFFA9BCC9),
    outline = Color(0xFF3B4F5E)
)

private val CarTypography = Typography(
    headlineMedium = Typography().headlineMedium.copy(fontWeight = FontWeight.SemiBold, fontSize = 30.sp),
    titleLarge = Typography().titleLarge.copy(fontWeight = FontWeight.SemiBold, fontSize = 24.sp),
    titleMedium = Typography().titleMedium.copy(fontWeight = FontWeight.Medium, fontSize = 19.sp),
    bodyLarge = Typography().bodyLarge.copy(fontSize = 18.sp),
    bodyMedium = Typography().bodyMedium.copy(fontSize = 16.sp),
    labelLarge = Typography().labelLarge.copy(fontSize = 16.sp),
    labelMedium = Typography().labelMedium.copy(fontSize = 14.sp)
)

@Composable
fun CarRelayTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = CarColors, typography = CarTypography, content = content)
}
