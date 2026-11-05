package com.local.carhome

import android.app.ActivityOptions
import android.content.Context
import android.content.Intent
import android.content.pm.LauncherApps
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Rect
import android.os.Process
import android.service.notification.NotificationListenerService
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap
import java.util.concurrent.ConcurrentHashMap

data class AppEntry(val pkg: String, val label: String)

/**
 * Launchable apps and how they open. Like CarPlay, every app opens in a window over the content area while the
 * Car Home rail stays on screen (Android freeform windows, enabled once over ADB). [front] is the app in that window.
 */
object Apps {
    private const val ME = "com.local.carhome"
    private val icons = ConcurrentHashMap<String, ImageBitmap>()
    private val NAV = listOf("app.organicmaps", "net.osmand.plus", "com.generalmagic.magicearth", "com.google.android.apps.maps", "com.waze")
    private val MUSIC = listOf("com.spotify.music", "com.google.android.apps.youtube.music", "deezer.android.app", "com.aspiro.tidal")

    /** Screen area beside the rail where apps open; set by MainActivity once it knows the screen and bar sizes. */
    var appArea: Rect? = null
    /** Package currently shown in the app window, or null while the dashboard is in front. */
    var front by mutableStateOf<String?>(null)

    fun all(ctx: Context): List<AppEntry> =
        ctx.getSystemService(LauncherApps::class.java).getActivityList(null, Process.myUserHandle())
            .filter { it.applicationInfo.packageName != ME }
            .map { AppEntry(it.applicationInfo.packageName, it.label.toString()) }
            .distinctBy { it.pkg }
            .sortedBy { it.label.lowercase() }

    fun cached(pkg: String): ImageBitmap? = icons[pkg]

    fun icon(ctx: Context, pkg: String, px: Int = 128): ImageBitmap? = icons[pkg] ?: runCatching {
        val d = ctx.getSystemService(LauncherApps::class.java).getActivityList(pkg, Process.myUserHandle()).firstOrNull()?.getIcon(0)
            ?: ctx.packageManager.getApplicationIcon(pkg)
        val b = Bitmap.createBitmap(px, px, Bitmap.Config.ARGB_8888)
        d.setBounds(0, 0, px, px); d.draw(Canvas(b))
        b.asImageBitmap().also { icons[pkg] = it }
    }.getOrNull()

    /** Dropped when an app is removed or updated, so a changed icon is redrawn. */
    fun forget(pkg: String) { icons.remove(pkg) }

    // Launch bounds only apply when the launch also asks for freeform mode; that setter is hidden, so it is looked up once.
    private const val WINDOWING_MODE_FREEFORM = 5
    private val setWindowingMode by lazy {
        runCatching { ActivityOptions::class.java.getMethod("setLaunchWindowingMode", Int::class.javaPrimitiveType) }.getOrNull()
    }

    /** Opens any activity (an app, a settings page) in the app window beside the rail. */
    fun open(ctx: Context, i: Intent, pkg: String? = i.`package` ?: i.component?.packageName): Boolean {
        i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_RESET_TASK_IF_NEEDED)
        val opts = appArea?.let { area ->
            ActivityOptions.makeBasic().setLaunchBounds(area).also { o -> runCatching { setWindowingMode?.invoke(o, WINDOWING_MODE_FREEFORM) } }.toBundle()
        }
        return runCatching { ctx.startActivity(i, opts) }.onSuccess { front = pkg }.isSuccess
    }

    fun launch(ctx: Context, pkg: String): Boolean = ctx.packageManager.getLaunchIntentForPackage(pkg)?.let { open(ctx, it, pkg) } ?: false

    /** Brings the dashboard back over any app window (the rail's Home button). */
    fun home(ctx: Context) {
        ctx.startActivity(Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_HOME).setPackage(ME).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
    }

    private fun prefs(ctx: Context) = ctx.getSharedPreferences("apps", Context.MODE_PRIVATE)

    /** The app the Navigate card opens: the driver's choice, else the first offline-capable nav app installed. */
    fun nav(ctx: Context, installed: Set<String>): String? = prefs(ctx).getString("nav", null)?.takeIf { it in installed } ?: NAV.firstOrNull { it in installed }
    fun setNav(ctx: Context, pkg: String) { prefs(ctx).edit().putString("nav", pkg).apply() }
    fun navCandidates(installed: Set<String>) = NAV.filter { it in installed }
    fun music(installed: Set<String>) = MUSIC.firstOrNull { it in installed }
}

/** Exists only so the home screen may read the active media session; it ignores notifications. */
class MediaListener : NotificationListenerService()
