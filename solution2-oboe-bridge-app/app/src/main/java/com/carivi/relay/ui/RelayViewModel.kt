package com.carivi.relay.ui

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.carivi.relay.CarRelayApp
import com.carivi.relay.RelayController
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

class RelayViewModel(app: Application) : AndroidViewModel(app) {
    val controller: RelayController = CarRelayApp.controller(app)
    val state: StateFlow<RelayController.DashboardState> = controller.state

    init {
        controller.start()
        // 1 Hz ticker: refreshes Oboe XRun counters, patch status and sample rates.
        viewModelScope.launch {
            while (isActive) {
                controller.tick()
                delay(1_000)
            }
        }
    }
}
