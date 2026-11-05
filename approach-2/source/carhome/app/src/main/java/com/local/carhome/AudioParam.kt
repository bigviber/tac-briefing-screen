package com.local.carhome

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.media.AudioManager

/**
 * Development aid for the call-audio work: passes key=value pairs to the audio HAL, as the Bluetooth app does in a call.
 *   adb shell am broadcast -n com.local.carhome/.AudioParam --es set "hfp_set_sampling_rate=16000;hfp_enable=true"
 *   adb shell am broadcast -n com.local.carhome/.AudioParam --es get "hfp_volume"
 * Only senders holding DUMP (the adb shell) can reach it; the result comes back in the broadcast's result data.
 */
class AudioParam : BroadcastReceiver() {
    override fun onReceive(c: Context, i: Intent) {
        val audio = c.getSystemService(AudioManager::class.java)
        i.getStringExtra("set")?.let { audio.setParameters(it); resultData = "set: $it" }
        i.getStringExtra("get")?.let { resultData = "get: " + audio.getParameters(it) }
    }
}
