package com.local.carhome

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.media.AudioDeviceCallback
import android.media.AudioDeviceInfo
import android.media.AudioManager
import android.os.Handler
import android.os.IBinder
import android.os.Looper

/**
 * Volume lock, per sound (media, calls, ringtone, alarms): a locked sound is held at its chosen step. Any change (buttons,
 * a stray touch, an app, a phone or car connecting with its own level) is put back within a moment.
 */
object VolumeLock {
    private const val PREFS = "volume_lock"
    private fun prefs(c: Context) = c.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
    /** The sounds that can be held. Media keeps its original keys, so a lock set before 1.6 survives the update. */
    val STREAMS = intArrayOf(AudioManager.STREAM_MUSIC, AudioManager.STREAM_VOICE_CALL, AudioManager.STREAM_RING, AudioManager.STREAM_ALARM)
    private fun lockKey(s: Int) = if (s == AudioManager.STREAM_MUSIC) "locked" else "locked_$s"
    private fun levelKey(s: Int) = if (s == AudioManager.STREAM_MUSIC) "level" else "level_$s"

    fun locked(c: Context, stream: Int = AudioManager.STREAM_MUSIC) = prefs(c).getBoolean(lockKey(stream), false)
    fun level(c: Context, stream: Int = AudioManager.STREAM_MUSIC) = prefs(c).getInt(levelKey(stream), -1)
    private fun anyLocked(c: Context) = STREAMS.any { locked(c, it) }

    fun lock(c: Context, level: Int, stream: Int = AudioManager.STREAM_MUSIC) {
        prefs(c).edit().putBoolean(lockKey(stream), true).putInt(levelKey(stream), level).apply()
        c.startForegroundService(Intent(c, VolumeLockService::class.java))
    }

    fun unlock(c: Context, stream: Int = AudioManager.STREAM_MUSIC) {
        prefs(c).edit().putBoolean(lockKey(stream), false).apply()
        if (!anyLocked(c)) c.stopService(Intent(c, VolumeLockService::class.java))
    }

    /** Called at boot and when the app starts: brings the service back if any lock was on. */
    fun resume(c: Context) {
        if (anyLocked(c)) c.startForegroundService(Intent(c, VolumeLockService::class.java))
    }
}

class VolumeLockService : Service() {
    private lateinit var audio: AudioManager
    private val main = Handler(Looper.getMainLooper())

    private val enforce = Runnable {
        for (s in VolumeLock.STREAMS) {
            val want = VolumeLock.level(this, s)
            if (VolumeLock.locked(this, s) && want >= 0 && audio.getStreamVolume(s) != want) runCatching { audio.setStreamVolume(s, want, 0) }
        }
    }

    private fun soon(delayMs: Long = 120) { main.removeCallbacks(enforce); main.postDelayed(enforce, delayMs) }

    private val changes = object : BroadcastReceiver() {
        override fun onReceive(c: Context, i: Intent) { soon() }
    }

    // A new output (car Bluetooth, USB audio) arrives with its own stored volume; put ours back once it settles.
    private val devices = object : AudioDeviceCallback() {
        override fun onAudioDevicesAdded(added: Array<out AudioDeviceInfo>) { soon(600); soon(1500) }
        override fun onAudioDevicesRemoved(removed: Array<out AudioDeviceInfo>) { soon(600) }
    }

    override fun onCreate() {
        super.onCreate()
        audio = getSystemService(AudioManager::class.java)
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel("lock", getString(R.string.volume_lock), NotificationManager.IMPORTANCE_MIN).apply {
            setShowBadge(false)
        })
        startForeground(1, Notification.Builder(this, "lock")
            .setSmallIcon(R.drawable.ic_car)
            .setContentTitle(getString(R.string.volume_lock))
            .setContentText("Locked volumes are held where you set them")
            .setOngoing(true)
            .build())
        registerReceiver(changes, IntentFilter("android.media.VOLUME_CHANGED_ACTION"))
        audio.registerAudioDeviceCallback(devices, main)
        soon(0)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (VolumeLock.STREAMS.none { VolumeLock.locked(this, it) }) { stopSelf(); return START_NOT_STICKY }
        soon(0)
        return START_STICKY
    }

    override fun onDestroy() {
        unregisterReceiver(changes)
        audio.unregisterAudioDeviceCallback(devices)
        main.removeCallbacks(enforce)
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null
}

class BootReceiver : BroadcastReceiver() {
    override fun onReceive(c: Context, i: Intent) { VolumeLock.resume(c) }
}
