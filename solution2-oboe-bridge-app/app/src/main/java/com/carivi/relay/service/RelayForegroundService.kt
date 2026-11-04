package com.carivi.relay.service

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.IBinder
import android.util.Log
import com.carivi.relay.CarRelayApp
import com.carivi.relay.MainActivity
import com.carivi.relay.R
import com.carivi.relay.RelayController
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.launch

/**
 * Keeps the relay alive in the car. Started by service.sh at boot, by
 * PowerEventReceiver on car power, and by the dashboard.
 */
class RelayForegroundService : Service() {
    private lateinit var controller: RelayController
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private var statusJob: Job? = null

    override fun onCreate() {
        super.onCreate()
        controller = CarRelayApp.controller(this)
        controller.start()
        createChannel()
        startForeground(
            NOTIFICATION_ID,
            buildNotification("starting"),
            ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK
        )
        statusJob = scope.launch {
            controller.state.collectLatest { s ->
                val text = "${s.mode.title} · ${s.engineStatus.describe()}"
                getSystemService(NotificationManager::class.java).notify(NOTIFICATION_ID, buildNotification(text))
            }
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_POWER_CONNECTED -> controller.onPowerConnected()
            ACTION_POWER_DISCONNECTED -> controller.onPowerDisconnected()
            ACTION_ADAPTER_ON -> controller.onAdapterOn()
            ACTION_START_RELAY -> controller.startRelay()
            ACTION_STOP_RELAY -> controller.stopRelay()
            ACTION_SEQUENCE -> controller.runSequence()
            ACTION_BOOT, null -> Log.i(TAG, "service up (${intent?.action ?: "explicit start"})")
        }
        return START_STICKY
    }

    override fun onDestroy() {
        statusJob?.cancel()
        scope.cancel()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun createChannel() {
        val nm = getSystemService(NotificationManager::class.java)
        if (nm.getNotificationChannel(CHANNEL_ID) == null) {
            nm.createNotificationChannel(
                NotificationChannel(CHANNEL_ID, getString(R.string.notif_channel_name), NotificationManager.IMPORTANCE_LOW).apply {
                    description = getString(R.string.notif_channel_desc)
                    setShowBadge(false)
                }
            )
        }
    }

    private fun buildNotification(text: String): Notification {
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        val stop = PendingIntent.getService(
            this, 1, Intent(this, RelayForegroundService::class.java).setAction(ACTION_STOP_RELAY),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        return Notification.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_launcher)
            .setContentTitle(getString(R.string.notif_title))
            .setContentText(text)
            .setContentIntent(open)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .addAction(Notification.Action.Builder(null, "Stop relay", stop).build())
            .build()
    }

    companion object {
        private const val TAG = "CarRelay.Service"
        private const val CHANNEL_ID = "carrelay.status"
        private const val NOTIFICATION_ID = 0x5EA7

        const val ACTION_BOOT = "com.carivi.relay.action.BOOT"
        const val ACTION_POWER_CONNECTED = "com.carivi.relay.action.POWER_CONNECTED"
        const val ACTION_POWER_DISCONNECTED = "com.carivi.relay.action.POWER_DISCONNECTED"
        const val ACTION_ADAPTER_ON = "com.carivi.relay.action.ADAPTER_ON"
        const val ACTION_START_RELAY = "com.carivi.relay.action.START_RELAY"
        const val ACTION_STOP_RELAY = "com.carivi.relay.action.STOP_RELAY"
        const val ACTION_SEQUENCE = "com.carivi.relay.action.SEQUENCE"
    }
}
