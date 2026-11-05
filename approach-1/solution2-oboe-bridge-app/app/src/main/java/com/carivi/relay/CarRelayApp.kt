package com.carivi.relay

import android.app.Application
import android.content.Context
import android.content.Intent
import android.util.Log
import com.carivi.relay.service.RelayForegroundService

class CarRelayApp : Application() {

    override fun onCreate() {
        super.onCreate()
        controller(this).start()
        try {
            startForegroundService(Intent(this, RelayForegroundService::class.java).setAction(RelayForegroundService.ACTION_BOOT))
        } catch (t: Throwable) {
            Log.w("CarRelay.App", "could not start relay service from Application", t)
        }
    }

    companion object {
        @Volatile private var instance: RelayController? = null

        fun controller(context: Context): RelayController =
            instance ?: synchronized(this) {
                instance ?: RelayController(context.applicationContext).also { instance = it }
            }
    }
}
