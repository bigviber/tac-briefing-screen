package com.local.carhome

import android.content.Context
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.hardware.display.DisplayManager
import android.os.Handler
import android.os.Looper
import android.view.Display
import android.view.Surface
import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.Spring
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.spring
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.Stable
import androidx.compose.runtime.derivedStateOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshotFlow
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.CompositingStrategy
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.text.drawText
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.rememberTextMeasurer
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.flow.collectLatest
import kotlin.math.abs
import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.roundToInt
import kotlin.math.sin

private const val COURSE_GAIN = 0.2f       // how much of the GPS / gyro difference is corrected per GPS fix
private const val MAX_TURN_PER_FIX = 6f    // above this the car is turning and GPS course lags: leave the heading to the gyro
private const val MIN_STEP = 0.25f         // smaller changes are not published, so a parked car redraws nothing
private const val PREF_LAST = "heading"

/** Shortest signed way from one bearing to another, -180..180. */
private fun turn(degrees: Float): Float { val d = (degrees + 540f) % 360f; return (if (d < 0f) d + 360f else d) - 180f }

/**
 * Where the car points. This tablet has no magnetometer (accelerometer and gyroscope only), so it cannot sense north:
 * GPS course sets the direction once the car moves, and the gyro-based Game Rotation Vector holds it through turns and
 * at stops. Only yaw changes come from the sensor, so the mount angle does not matter.
 */
@Stable
class Heading(ctx: Context) {
    enum class State { NONE, LAST, LIVE }

    private val sensors = ctx.getSystemService(SensorManager::class.java)
    private val sensor: Sensor? = sensors.getDefaultSensor(Sensor.TYPE_GAME_ROTATION_VECTOR)
    private val display: Display? = ctx.getSystemService(DisplayManager::class.java).getDisplay(Display.DEFAULT_DISPLAY)
    private val prefs = ctx.getSharedPreferences("ui", Context.MODE_PRIVATE)
    private val main = Handler(Looper.getMainLooper())

    /** Bearing in degrees, 0..360. Read it in the draw phase or through derivedStateOf: it can change many times a second. */
    var degrees by mutableFloatStateOf(0f); private set
    var state by mutableStateOf(State.NONE); private set

    private val matrix = FloatArray(9)
    private var rotation = Surface.ROTATION_0
    private var yaw = Float.NaN
    private var offset = Float.NaN
    private var yawAtLastFix = Float.NaN

    private val listener = object : SensorEventListener {
        override fun onAccuracyChanged(s: Sensor?, accuracy: Int) {}
        override fun onSensorChanged(e: SensorEvent) {
            SensorManager.getRotationMatrixFromVector(matrix, e.values)
            // The screen's up axis in the world frame, plus the direction out of the back of the tablet: flat, upright or
            // anywhere between, their sum stays level and points the way the tablet faces, with no jump as the tilt changes.
            val sideways = rotation == Surface.ROTATION_90 || rotation == Surface.ROTATION_270
            val sign = if (rotation == Surface.ROTATION_90 || rotation == Surface.ROTATION_180) -1f else 1f
            val axis = if (sideways) 0 else 1
            yaw = Math.toDegrees(atan2((sign * matrix[axis] - matrix[2]).toDouble(), (sign * matrix[3 + axis] - matrix[5]).toDouble())).toFloat()
            if (offset.isNaN() && state == State.LAST) offset = degrees - yaw
            publish()
        }
    }

    /** A GPS course from a moving car. The first one sets the heading; later ones only trim gyro drift, and not mid-turn. */
    fun onCourse(course: Float) {
        if (yaw.isNaN() && sensor != null) return
        val y = if (yaw.isNaN()) 0f else yaw
        if (state != State.LIVE || offset.isNaN()) {
            offset = course - y
            state = State.LIVE
        } else if (yawAtLastFix.isNaN() || abs(turn(y - yawAtLastFix)) < MAX_TURN_PER_FIX) {
            offset += turn(course - (y + offset)) * COURSE_GAIN
        }
        yawAtLastFix = y
        publish()
    }

    private fun publish() {
        if (offset.isNaN()) return
        val d = ((if (yaw.isNaN()) 0f else yaw) + offset + 720f) % 360f
        if (abs(turn(d - degrees)) >= MIN_STEP) degrees = d
    }

    fun start() {
        rotation = display?.rotation ?: Surface.ROTATION_0
        yaw = Float.NaN; offset = Float.NaN; yawAtLastFix = Float.NaN
        if (prefs.contains(PREF_LAST)) { degrees = prefs.getFloat(PREF_LAST, 0f); state = State.LAST } else state = State.NONE
        sensor?.let { sensors.registerListener(listener, it, SENSOR_PERIOD_US, main) }
    }

    fun stop() {
        sensors.unregisterListener(listener)
        if (state != State.NONE) prefs.edit().putFloat(PREF_LAST, degrees).apply()
    }

    private companion object { const val SENSOR_PERIOD_US = 66_000 }   // about 15 Hz: the dial's spring fills in the frames between
}

/** The dashboard's compass: a dial that turns under a fixed mark, with the direction spelled out beside it. */
@Composable
fun CompassCard(h: Heading, modifier: Modifier) {
    Row(modifier.clip(RoundedCornerShape(28.dp)).background(Card).padding(18.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(22.dp)) {
        CompassDial(h, Modifier.fillMaxHeight().aspectRatio(1f))
        Column(Modifier.weight(1f).fillMaxHeight()) {
            Txt("Compass", t(20f, TextHi, FontWeight.Medium))
            Spacer(Modifier.weight(1f))
            HeadingText(h)
        }
    }
}

/** Recomposes only when the whole-degree value changes; the dial itself turns in its own layer. */
@Composable
private fun HeadingText(h: Heading) {
    val whole by remember(h) { derivedStateOf { h.degrees.roundToInt() % 360 } }
    val known = h.state != Heading.State.NONE
    Txt(if (known) compass(whole.toFloat()) else "\u2013", t(52f, if (known) TextHi else TextLo, FontWeight.SemiBold, ls = -1f))
    if (known) Txt("$whole\u00B0", t(22f, TextMid, FontWeight.Medium, tabular = true))
    Spacer(Modifier.height(6.dp))
    Txt(when (h.state) {
        Heading.State.LIVE -> "GPS heading"
        Heading.State.LAST -> "Last heading"
        Heading.State.NONE -> "Updates when you drive"
    }, t(14f, TextLo))
}

@Composable
private fun CompassDial(h: Heading, modifier: Modifier) {
    val measurer = rememberTextMeasurer()
    val labels = remember {
        listOf(0, 90, 180, 270).zip(listOf("N", "E", "S", "W")).map { (a, s) ->
            a to measurer.measure(s, if (a == 0) t(19f, Sky, FontWeight.SemiBold) else t(16f, TextMid, FontWeight.Medium))
        }
    }
    val angle = remember { Animatable(h.degrees) }
    // A spring picks up from the current speed each time a new reading interrupts it, so 15 readings a second look continuous.
    LaunchedEffect(h) {
        snapshotFlow { h.degrees }.collectLatest { d ->
            angle.animateTo(angle.value + turn(d - angle.value), spring(dampingRatio = 1f, stiffness = Spring.StiffnessMediumLow))
        }
    }
    val fade by animateFloatAsState(if (h.state == Heading.State.NONE) 0.4f else 1f, tween(400), label = "compass")
    Box(modifier, contentAlignment = Alignment.Center) {
        Canvas(Modifier.fillMaxSize()) { drawCircle(Panel) }
        // Drawn once into a cached layer; each frame only changes the layer's rotation, so turning redraws nothing.
        Canvas(Modifier.fillMaxSize().graphicsLayer { rotationZ = -angle.value; alpha = fade; compositingStrategy = CompositingStrategy.Offscreen }) {
            val r = size.minDimension / 2; val c = Offset(size.width / 2, size.height / 2)
            for (k in 0 until 72) {
                val a = Math.toRadians(k * 5.0 - 90.0)
                val north = k == 0; val cardinal = k % 18 == 0; val major = k % 6 == 0
                val r1 = r - 5.dp.toPx(); val r2 = r1 - (if (cardinal) 12.dp else if (major) 9.dp else 5.dp).toPx()
                drawLine(if (north) Sky else if (cardinal) TextHi else if (major) TextMid else TextLo,
                    Offset(c.x + r1 * cos(a).toFloat(), c.y + r1 * sin(a).toFloat()), Offset(c.x + r2 * cos(a).toFloat(), c.y + r2 * sin(a).toFloat()),
                    (if (cardinal) 2.4f else if (major) 1.6f else 1f).dp.toPx(), StrokeCap.Round)
            }
            val rl = r - 32.dp.toPx()
            for ((d, layout) in labels) {
                val a = Math.toRadians(d - 90.0)
                drawText(layout, topLeft = Offset(c.x + rl * cos(a).toFloat() - layout.size.width / 2f, c.y + rl * sin(a).toFloat() - layout.size.height / 2f))
            }
        }
        Canvas(Modifier.fillMaxSize()) {   // the fixed mark: straight ahead
            val cx = size.width / 2; val w = 7.dp.toPx(); val tip = 13.dp.toPx()
            drawPath(Path().apply { moveTo(cx - w, 0f); lineTo(cx + w, 0f); lineTo(cx, tip); close() }, if (h.state == Heading.State.NONE) TextLo else Sky)
        }
        Glyph(G.Nav, 30.dp, if (h.state == Heading.State.NONE) TextLo else Sky)
    }
}
