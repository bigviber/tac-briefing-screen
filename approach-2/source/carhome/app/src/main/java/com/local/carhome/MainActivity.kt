package com.local.carhome

import android.content.pm.LauncherApps
import android.content.res.Configuration
import android.graphics.Rect
import android.os.Bundle
import android.os.UserHandle
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.OnBackPressedCallback
import androidx.activity.compose.setContent
import androidx.compose.runtime.mutableStateOf
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/** Width of the rail (padding + buttons) that stays visible beside app windows. */
const val RAIL_DP = 116

class MainActivity : ComponentActivity() {
    private lateinit var car: CarState
    private lateinit var bt: BtLink
    private lateinit var lib: PhoneLibrary
    private lateinit var calls: CallLink
    private val apps = mutableStateOf<List<AppEntry>>(emptyList())

    // The app list is read once, then again only when an app is installed, removed, updated, enabled or disabled.
    private val packages = object : LauncherApps.Callback() {
        override fun onPackageAdded(pkg: String, user: UserHandle) = reload(pkg)
        override fun onPackageRemoved(pkg: String, user: UserHandle) = reload(pkg)
        override fun onPackageChanged(pkg: String, user: UserHandle) = reload(pkg)
        override fun onPackagesAvailable(pkgs: Array<out String>, user: UserHandle, replacing: Boolean) = reload()
        override fun onPackagesUnavailable(pkgs: Array<out String>, user: UserHandle, replacing: Boolean) = reload()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)   // the screen never sleeps while home shows
        WindowCompat.setDecorFitsSystemWindows(window, false)
        hideBars()
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) { override fun handleOnBackPressed() {} })
        car = CarState(applicationContext)
        bt = BtLink(applicationContext)
        lib = PhoneLibrary(applicationContext)
        calls = CallLink(applicationContext).also { it.start() }   // for the app's whole life: a call can wake the screen
        VolumeLock.resume(this)
        getSystemService(LauncherApps::class.java).registerCallback(packages)
        reload()
        measureAppArea()
        setContent { CarHome(car, bt, lib, calls, apps.value) }
    }

    /** Apps open right of the rail, between the status bar and the 3-button navigation bar (both show while an app is up). */
    private fun measureAppArea() {
        val screen = windowManager.currentWindowMetrics.bounds
        val d = resources.displayMetrics.density
        fun sys(name: String, fallbackDp: Int): Int {
            val id = resources.getIdentifier(name, "dimen", "android")
            return if (id != 0) resources.getDimensionPixelSize(id) else (fallbackDp * d).toInt()
        }
        Apps.appArea = Rect((RAIL_DP * d).toInt(), sys("status_bar_height", 24), screen.right, screen.bottom - sys("navigation_bar_height", 48))
    }

    override fun onConfigurationChanged(newConfig: Configuration) { super.onConfigurationChanged(newConfig); measureAppArea() }
    override fun onResume() { super.onResume(); Apps.front = null }

    private fun reload(changed: String? = null) {
        changed?.let(Apps::forget)
        lifecycleScope.launch {
            apps.value = withContext(Dispatchers.Default) { Apps.all(applicationContext) }
        }
    }

    private fun hideBars() = WindowInsetsControllerCompat(window, window.decorView).run {
        hide(WindowInsetsCompat.Type.systemBars())
        systemBarsBehavior = WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
    }

    override fun onStart() { super.onStart(); car.start(); bt.start(); lib.start() }
    override fun onStop() { lib.stop(); bt.stop(); car.stop(); super.onStop() }
    override fun onWindowFocusChanged(hasFocus: Boolean) { super.onWindowFocusChanged(hasFocus); if (hasFocus) hideBars() }

    override fun onDestroy() {
        getSystemService(LauncherApps::class.java).unregisterCallback(packages)
        calls.stop()
        super.onDestroy()
    }
}
