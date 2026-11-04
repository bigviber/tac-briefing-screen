package com.carivi.relay

import android.Manifest
import android.content.pm.PackageManager
import android.os.Bundle
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.viewModels
import androidx.core.content.ContextCompat
import com.carivi.relay.ui.CarRelayDashboardUI
import com.carivi.relay.ui.CarRelayTheme
import com.carivi.relay.ui.RelayViewModel

class MainActivity : ComponentActivity() {
    private val viewModel: RelayViewModel by viewModels()

    private val recordAudioRequest =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            viewModel.controller.log("RECORD_AUDIO ${if (granted) "granted" else "DENIED - Mode 2 loop-back ingress unavailable"}")
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            recordAudioRequest.launch(Manifest.permission.RECORD_AUDIO)
        }
        setContent {
            CarRelayTheme {
                CarRelayDashboardUI(viewModel)
            }
        }
    }
}
