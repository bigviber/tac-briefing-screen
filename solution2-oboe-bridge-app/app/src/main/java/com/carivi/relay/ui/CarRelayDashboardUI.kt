package com.carivi.relay.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Pause
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.SkipNext
import androidx.compose.material.icons.filled.SkipPrevious
import androidx.compose.material3.AssistChip
import androidx.compose.material3.AssistChipDefaults
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ExposedDropdownMenuBox
import androidx.compose.material3.ExposedDropdownMenuDefaults
import androidx.compose.material3.FilledIconButton
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButtonDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.RadioButton
import androidx.compose.material3.SegmentedButton
import androidx.compose.material3.SegmentedButtonDefaults
import androidx.compose.material3.SingleChoiceSegmentedButtonRow
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.carivi.relay.RelayController
import com.carivi.relay.RelayController.DashboardState
import com.carivi.relay.audiopatch.AudioPatchBridge
import com.carivi.relay.core.BtConstants
import com.carivi.relay.core.EngineStatus
import com.carivi.relay.core.RelayMode

/**
 * Landscape head-unit dashboard.
 *
 *  ┌──────────────┬────────────────────┬──────────────┐
 *  │ Architecture │ Device role cards  │ System / log │
 *  │ switcher     │ + live profile     │              │
 *  │ + sequencer  │ + engine counters  │              │
 *  ├──────────────┴────────────────────┴──────────────┤
 *  │ iPhone Now Playing bar  ◄◄   ►/❚❚   ►►            │
 *  └──────────────────────────────────────────────────┘
 */
@Composable
fun CarRelayDashboardUI(viewModel: RelayViewModel) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    val controller = viewModel.controller

    Surface(modifier = Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
        Column(modifier = Modifier.fillMaxSize().padding(12.dp)) {
            HeaderBar(state)
            Spacer(Modifier.height(8.dp))
            Row(modifier = Modifier.weight(1f).fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                ArchitectureColumn(state, controller, Modifier.weight(1.05f).fillMaxHeight())
                DevicesColumn(state, controller, Modifier.weight(1.35f).fillMaxHeight())
                SystemColumn(state, controller, Modifier.weight(1.0f).fillMaxHeight())
            }
            Spacer(Modifier.height(8.dp))
            NowPlayingBar(state, controller)
        }
    }
}

// ---------------------------------------------------------------------------
// Header
// ---------------------------------------------------------------------------
@Composable
private fun HeaderBar(state: DashboardState) {
    Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
        Text("Car Relay", style = MaterialTheme.typography.headlineMedium)
        Spacer(Modifier.width(16.dp))
        Text(
            "iPhone ⇄ Lenovo TB-J606F ⇄ Car · SDK 30",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )
        Spacer(Modifier.weight(1f))
        StatusPill(state.engineStatus)
    }
}

@Composable
private fun StatusPill(status: EngineStatus) {
    val (label, color) = when (status) {
        EngineStatus.Idle -> "IDLE" to MaterialTheme.colorScheme.outline
        is EngineStatus.Starting -> "STARTING" to MaterialTheme.colorScheme.tertiary
        is EngineStatus.Running -> "RELAYING" to Color(0xFF66BB6A)
        is EngineStatus.Degraded -> "DEGRADED" to MaterialTheme.colorScheme.tertiary
        is EngineStatus.Failed -> "FAILED" to MaterialTheme.colorScheme.error
    }
    Box(
        modifier = Modifier
            .background(color.copy(alpha = 0.18f), RoundedCornerShape(50))
            .padding(horizontal = 14.dp, vertical = 6.dp)
    ) {
        Text(label, color = color, fontWeight = FontWeight.Bold, style = MaterialTheme.typography.labelLarge)
    }
}

// ---------------------------------------------------------------------------
// Left: architecture switcher + sequencer
// ---------------------------------------------------------------------------
@Composable
private fun ArchitectureColumn(state: DashboardState, controller: RelayController, modifier: Modifier) {
    Column(modifier = modifier.verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        DashCard(title = "Live architecture") {
            RelayMode.entries.forEach { mode ->
                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    modifier = Modifier.fillMaxWidth().padding(vertical = 2.dp)
                ) {
                    RadioButton(selected = state.mode == mode, onClick = { controller.selectMode(mode) })
                    Column(Modifier.padding(start = 4.dp)) {
                        Text(mode.title, style = MaterialTheme.typography.titleMedium)
                        Text(mode.subtitle, style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                }
            }
            HorizontalDivider(Modifier.padding(vertical = 6.dp))
            when (state.mode) {
                RelayMode.OBOE_BRIDGE -> ToggleRow(
                    "Force uid loop-back ingress", state.forceLoopback
                ) { controller.setForceLoopback(it) }
                RelayMode.AUDIO_PATCH -> {
                    Text("Mode 3 strategy", style = MaterialTheme.typography.labelMedium)
                    SingleChoiceSegmentedButtonRow(Modifier.fillMaxWidth()) {
                        AudioPatchBridge.Strategy.entries.forEachIndexed { i, s ->
                            SegmentedButton(
                                selected = state.patchStrategy == s,
                                onClick = { controller.setPatchStrategy(s) },
                                shape = SegmentedButtonDefaults.itemShape(i, AudioPatchBridge.Strategy.entries.size)
                            ) {
                                Text(if (s == AudioPatchBridge.Strategy.DEVICE_PATCH) "Device patch" else "UID render mix", maxLines = 1)
                            }
                        }
                    }
                }
                RelayMode.NATIVE_FOCUS -> Text(
                    "Requires CarIvIBtOverlay (auto audio focus) or a Now Playing ▶ to arm the sink stream.",
                    style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
            Spacer(Modifier.height(8.dp))
            Text(state.engineStatus.describe(), style = MaterialTheme.typography.bodyMedium, maxLines = 3, overflow = TextOverflow.Ellipsis)
            Spacer(Modifier.height(8.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(onClick = { controller.startRelay() }, enabled = !state.relayArmed || state.engineStatus is EngineStatus.Failed, modifier = Modifier.weight(1f)) {
                    Text("Start relay")
                }
                OutlinedButton(onClick = { controller.stopRelay() }, enabled = state.relayArmed, modifier = Modifier.weight(1f)) {
                    Text("Stop")
                }
            }
        }

        DashCard(title = "Connection sequencer") {
            Text(
                "① A2DP source → car  ② settle ${com.carivi.relay.bt.BtRoleCoordinator.SETTLE_MS} ms  ③ A2DP sink ← iPhone  ④ active device = car",
                style = MaterialTheme.typography.labelMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
            Spacer(Modifier.height(8.dp))
            Button(onClick = { controller.runSequence() }, enabled = !state.sequencerBusy, modifier = Modifier.fillMaxWidth()) {
                Text(if (state.sequencerBusy) "Sequencing…" else "Connect sequence + relay")
            }
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.padding(top = 6.dp)) {
                FilledTonalButton(onClick = { controller.pinRoles() }, modifier = Modifier.weight(1f)) { Text("Pin roles") }
                OutlinedButton(onClick = { controller.disconnectAll() }, modifier = Modifier.weight(1f)) { Text("Disconnect") }
            }
            HorizontalDivider(Modifier.padding(vertical = 6.dp))
            ToggleRow("Auto-sequence on car power", state.autoSequenceOnPower) { controller.setAutoSequence(it) }
            ToggleRow("HFP client ← iPhone (calls)", state.hfpClientEnabled) { controller.setHfpClient(it) }
            ToggleRow("HFP AG → car", state.carHfpAgEnabled) { controller.setCarHfpAg(it) }
        }
    }
}

// ---------------------------------------------------------------------------
// Middle: device role cards + engine counters
// ---------------------------------------------------------------------------
@Composable
private fun DevicesColumn(state: DashboardState, controller: RelayController, modifier: Modifier) {
    Column(modifier = modifier.verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        val p = state.profiles
        DeviceCard(
            title = "iPhone · Ingress Sink",
            subtitle = "we are A2DP Sink · AVRCP CT · HFP HF",
            selected = state.iphoneAddress,
            bonded = state.bonded,
            onSelect = { controller.selectIphone(it) },
            chips = listOf(
                "A2DP_SINK" to p.a2dpSinkIphone,
                "AVRCP_CONTROLLER" to p.avrcpControllerIphone,
                "HFP_CLIENT" to p.hfpClientIphone
            ),
            footer = if (p.sinkPlaying) "stream PLAYING" else "stream idle"
        )
        DeviceCard(
            title = "Car stereo · Egress Source",
            subtitle = "we are A2DP Source" + if (state.carHfpAgEnabled) " · HFP AG" else "",
            selected = state.carAddress,
            bonded = state.bonded,
            onSelect = { controller.selectCar(it) },
            chips = listOf(
                "A2DP_SOURCE" to p.a2dpSourceCar,
                "HFP_AG" to p.hfpAgCar
            ),
            footer = buildString {
                append("active: ").append(p.activeA2dpAddress ?: "none")
                p.carCodec?.let { append(" · ").append(it) }
                if (p.carCodecSampleRate > 0) append(" @ ").append(p.carCodecSampleRate).append(" Hz")
            }
        )
        DashCard(title = "Engine counters · ${state.mode.title}") {
            val s = state.stats
            StatGrid(
                listOf(
                    "In rate" to (if (s.inputSampleRate > 0) "${s.inputSampleRate} Hz" else "—"),
                    "Out rate" to (if (s.outputSampleRate > 0) "${s.outputSampleRate} Hz" else "—"),
                    "Channels" to (if (s.channelCount > 0) "${s.channelCount}" else "—"),
                    "XRuns in/out" to "${s.inputXRuns} / ${s.outputXRuns}",
                    "Ring fill" to "${s.bufferFillFrames} f",
                    "Slips" to "${s.slipCorrections}",
                    "Latency" to (if (s.latencyMs > 0) String.format("%.1f ms", s.latencyMs) else "—"),
                    "Restarts" to "${s.restarts}"
                )
            )
            if (s.extra.isNotBlank()) {
                Text(s.extra, style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant, fontFamily = FontFamily.Monospace)
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun DeviceCard(
    title: String,
    subtitle: String,
    selected: String?,
    bonded: List<RelayController.BtDeviceUi>,
    onSelect: (String?) -> Unit,
    chips: List<Pair<String, Int>>,
    footer: String
) {
    DashCard(title = title, subtitle = subtitle) {
        var expanded by remember { mutableStateOf(false) }
        val label = bonded.firstOrNull { it.address.equals(selected, true) }?.let { "${it.name}  ${it.address}" }
            ?: selected ?: "Select paired device"
        ExposedDropdownMenuBox(expanded = expanded, onExpandedChange = { expanded = it }) {
            OutlinedTextField(
                value = label,
                onValueChange = {},
                readOnly = true,
                singleLine = true,
                trailingIcon = { ExposedDropdownMenuDefaults.TrailingIcon(expanded) },
                modifier = Modifier.menuAnchor().fillMaxWidth()
            )
            ExposedDropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }) {
                bonded.forEach { d ->
                    DropdownMenuItem(
                        text = { Text("${d.name}  ·  ${d.address}") },
                        onClick = { onSelect(d.address); expanded = false }
                    )
                }
                DropdownMenuItem(text = { Text("— clear —") }, onClick = { onSelect(null); expanded = false })
            }
        }
        Spacer(Modifier.height(8.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            chips.forEach { (name, st) -> ProfileChip(name, st) }
        }
        Spacer(Modifier.height(6.dp))
        Text(footer, style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

@Composable
private fun ProfileChip(name: String, state: Int) {
    val color = when (state) {
        BtConstants.STATE_CONNECTED -> Color(0xFF66BB6A)
        BtConstants.STATE_CONNECTING, BtConstants.STATE_DISCONNECTING -> MaterialTheme.colorScheme.tertiary
        else -> MaterialTheme.colorScheme.outline
    }
    AssistChip(
        onClick = {},
        label = { Text("$name · ${BtConstants.stateName(state)}", style = MaterialTheme.typography.labelMedium) },
        colors = AssistChipDefaults.assistChipColors(containerColor = color.copy(alpha = 0.16f), labelColor = color)
    )
}

@Composable
private fun StatGrid(items: List<Pair<String, String>>) {
    items.chunked(4).forEach { row ->
        Row(Modifier.fillMaxWidth().padding(vertical = 3.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            row.forEach { (k, v) ->
                Column(Modifier.weight(1f)) {
                    Text(k, style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    Text(v, style = MaterialTheme.typography.titleMedium, fontFamily = FontFamily.Monospace)
                }
            }
            repeat(4 - row.size) { Spacer(Modifier.weight(1f)) }
        }
    }
}

// ---------------------------------------------------------------------------
// Right: system + log
// ---------------------------------------------------------------------------
@Composable
private fun SystemColumn(state: DashboardState, controller: RelayController, modifier: Modifier) {
    Column(modifier = modifier, verticalArrangement = Arrangement.spacedBy(10.dp)) {
        DashCard(title = "System") {
            Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                FlagChip("root", state.rootAvailable)
                FlagChip("priv-app", state.privileged)
                FlagChip("adapter", state.profiles.adapterOn)
            }
            Spacer(Modifier.height(8.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                FilledTonalButton(onClick = { controller.applyBootstrap() }, modifier = Modifier.weight(1f)) { Text("Activate stack") }
                OutlinedButton(onClick = { controller.refreshDiagnostics() }, modifier = Modifier.weight(1f)) { Text("Diagnostics") }
            }
        }
        DashCard(title = "Log", modifier = Modifier.weight(1f)) {
            val listState = rememberLazyListState()
            LaunchedEffect(state.log.size) {
                if (state.log.isNotEmpty()) listState.animateScrollToItem(state.log.size - 1)
            }
            LazyColumn(state = listState, modifier = Modifier.fillMaxSize()) {
                items(state.log) { line ->
                    Text(line, style = MaterialTheme.typography.labelMedium, fontFamily = FontFamily.Monospace, fontSize = 12.sp, lineHeight = 15.sp)
                }
            }
        }
    }
}

@Composable
private fun FlagChip(name: String, ok: Boolean) {
    val color = if (ok) Color(0xFF66BB6A) else MaterialTheme.colorScheme.error
    AssistChip(
        onClick = {},
        label = { Text(name) },
        colors = AssistChipDefaults.assistChipColors(containerColor = color.copy(alpha = 0.16f), labelColor = color)
    )
}

// ---------------------------------------------------------------------------
// Bottom: iPhone Now Playing (Media3 ↔ BluetoothMediaBrowserService)
// ---------------------------------------------------------------------------
@Composable
private fun NowPlayingBar(state: DashboardState, controller: RelayController) {
    val np = state.nowPlaying
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.primaryContainer)
    ) {
        Row(modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 10.dp), verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text(
                    np.title ?: if (np.connected) "Nothing playing" else "AVRCP browser not connected",
                    style = MaterialTheme.typography.titleLarge, maxLines = 1, overflow = TextOverflow.Ellipsis
                )
                Text(
                    listOfNotNull(np.artist, np.album).joinToString(" · ").ifEmpty { np.error ?: "iPhone · AVRCP 1.x metadata" },
                    style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onPrimaryContainer.copy(alpha = 0.8f),
                    maxLines = 1, overflow = TextOverflow.Ellipsis
                )
            }
            Row(horizontalArrangement = Arrangement.spacedBy(14.dp), verticalAlignment = Alignment.CenterVertically) {
                FilledIconButton(onClick = { controller.nowPlaying.previous() }, modifier = Modifier.size(64.dp)) {
                    Icon(Icons.Filled.SkipPrevious, contentDescription = "Previous", modifier = Modifier.size(36.dp))
                }
                FilledIconButton(
                    onClick = { controller.nowPlaying.togglePlayPause() },
                    modifier = Modifier.size(80.dp),
                    colors = IconButtonDefaults.filledIconButtonColors(containerColor = MaterialTheme.colorScheme.primary)
                ) {
                    Icon(
                        if (np.isPlaying) Icons.Filled.Pause else Icons.Filled.PlayArrow,
                        contentDescription = if (np.isPlaying) "Pause" else "Play",
                        modifier = Modifier.size(44.dp)
                    )
                }
                FilledIconButton(onClick = { controller.nowPlaying.next() }, modifier = Modifier.size(64.dp)) {
                    Icon(Icons.Filled.SkipNext, contentDescription = "Next", modifier = Modifier.size(36.dp))
                }
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Shared pieces
// ---------------------------------------------------------------------------
@Composable
private fun DashCard(
    title: String,
    subtitle: String? = null,
    modifier: Modifier = Modifier,
    content: @Composable () -> Unit
) {
    Card(modifier = modifier.fillMaxWidth(), colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)) {
        Column(Modifier.padding(14.dp)) {
            Text(title, style = MaterialTheme.typography.titleMedium)
            if (subtitle != null) {
                Text(subtitle, style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            Spacer(Modifier.height(8.dp))
            content()
        }
    }
}

@Composable
private fun ToggleRow(label: String, checked: Boolean, onChange: (Boolean) -> Unit) {
    Row(Modifier.fillMaxWidth().padding(vertical = 2.dp), verticalAlignment = Alignment.CenterVertically) {
        Text(label, style = MaterialTheme.typography.bodyMedium, modifier = Modifier.weight(1f))
        Switch(checked = checked, onCheckedChange = onChange)
    }
}
