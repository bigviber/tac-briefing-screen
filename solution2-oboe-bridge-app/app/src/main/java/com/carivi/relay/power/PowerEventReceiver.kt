package com.carivi.relay.power

import android.bluetooth.BluetoothAdapter
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log
import com.carivi.relay.service.RelayForegroundService

/**
 * Car power detection. ACTION_POWER_CONNECTED / _DISCONNECTED and BOOT_COMPLETED
 * are exempt from the implicit-broadcast restrictions, so a manifest receiver
 * still fires on Android 11. When the head unit's 12 V USB feed comes up the
 * connection sequencer runs; when it drops the relay is parked.
 */
class PowerEventReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val action = intent.action ?: return
        Log.i(TAG, "received $action")
        val serviceAction = when (action) {
            Intent.ACTION_POWER_CONNECTED -> RelayForegroundService.ACTION_POWER_CONNECTED
            Intent.ACTION_POWER_DISCONNECTED -> RelayForegroundService.ACTION_POWER_DISCONNECTED
            Intent.ACTION_BOOT_COMPLETED,
            Intent.ACTION_LOCKED_BOOT_COMPLETED -> RelayForegroundService.ACTION_BOOT
            BluetoothAdapter.ACTION_STATE_CHANGED -> {
                if (intent.getIntExtra(BluetoothAdapter.EXTRA_STATE, -1) == BluetoothAdapter.STATE_ON)
                    RelayForegroundService.ACTION_ADAPTER_ON else return
            }
            else -> return
        }
        val svc = Intent(context, RelayForegroundService::class.java).setAction(serviceAction)
        try {
            context.startForegroundService(svc)
        } catch (t: Throwable) {
            Log.w(TAG, "startForegroundService failed", t)
        }
    }

    companion object {
        private const val TAG = "CarRelay.Power"
    }
}
