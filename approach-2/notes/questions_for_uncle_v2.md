# CarRelay v1.0.1: test results and questions (TB-J606F)

## Setup under test
- Lenovo TB-J606F, S320311_230810_ROW (SDK 30), Magisk 30.7, SELinux enforcing.
- Modules: btdual_patch v5, btsink_overlay_prop v2, carrelay_ivi v1.0.1 (your build, unmodified, signed with our key), carhome_priv v1.6.2 (our launcher, priv-app), hfp_audio v1.
- libbluetooth_qti.so: stock sha1 bdfef236a142..., v5 sha1 da7e2e993afc... v5 = v1 (sink worker start) + v2 (no 3 s suspend) + v5a (the source BTA_AvRegister also creates the SNK SEPs, codecs 7-9, tsep 1, bte_av_sink_media_callback / bta_av_sink_data_cback). Bluetooth.apk v5: Config.addAudioProfiles keeps A2dpService when persist.vendor.service.bt.a2dp.sink=true.
- Props: persist.vendor.service.bt.a2dp.sink=true, persist.bluetooth.maxconnectedaudiodevices=2 (yours), offload cap sbc-aac.
- Remotes: iPhone (A2DP source to us, AVRCP TG, HFP AG, PBAP); Sony WH-1000XM6 standing in for the car (A2DP sink, AVRCP CT, HFP HF).

## What works
- All v1.0.1 fixes verified: clean first boot (no "Killing ... change com.android.bluetooth"), read-only service.sh with correct overlay report, hidden_api_policy at firmware default with 0 hidden-API denials for com.carivi.relay, no root, Unpin roles (all setConnectionPolicy calls return true).
- Phone -> tablet music when the iPhone presses Play: BTA_AV_START_EVT (index 0, peer_sep 0) -> BTIF_MEDIA_SINK_DECODER_UPDATE -> A2dpSinkStreamHandler shouldRequestFocus=true -> focus granted (your overlay) -> AudioFlinger track from uid 1002, 44.1 kHz stereo, 0 underruns. This happens while bt_btif logs "bta_av_co_get_active_peer: peer index out of bounds: 5", so on this QTI build the sink decoder does run without a bta_av_co active peer.

## A. Main blocker: the source role never connects at the Java layer
Evidence (XM6, tablet-initiated; logcat kept):
1. btif_av index 1: BTIF_AV_CONNECT_REQ_EVT -> API_OPEN -> SDP_DISC_OK -> AVDT_CONNECT -> STR_DISC_OK -> STR_GETCAP_OK x3 -> BTIF_AV_SOURCE_CONFIG_UPDATED_EVT -> STR_OPEN_OK -> BTA_AV_OPEN_EVT; opened handler logs "peer_sep: 1, index: 1" (the iPhone's slot logs "peer_sep: 0, index: 0"). Codec AAC 44.1 kHz. Native link is fine and the per-slot role is right.
2. Java: "BluetoothA2dpSinkServiceJni: bta2dp_connection_state_callback" -> "A2dpSinkService: onConnectionStateChanged. State = 1, device:<XM6>" -> "Maximum number of A2DP Sink Connections reached: 1" -> "State Machine not found for device. Return." A2dpService's A2dpStateMachine for the XM6 stays CONNECTING and times out every 30 s (loops via deferred CONNECTs). No BT A2DP output device ever appears in APM, so Modes 1/2/3 have nothing to route to.
3. With the iPhone disconnected (sink slot free), the XM6 connected as an A2DP SINK device plus an AvrcpControllerStateMachine (the XM6's BTA_AV_RC_OPEN_EVT on index 1, peer_sep 1, went to the CT side).
4. Your sequencer (Pin roles, car first, iPhone not connected): XM6 sink policy FORBIDDEN, A2DP source -> connecting -> "car A2DP source did not connect"; again only SinkServiceJni callbacks (State 3, then 0), none from BluetoothA2dpServiceJni.
5. Afterwards the iPhone's sink link would not reconnect (connectA2dpNative issued, no callback) until `svc bluetooth disable/enable`.

Disassembly of btif_report_connection_state(btav_connection_state_t, RawAddress*) at 0x1ba370 (identical in stock and v5):
```
ldr  x8, [0x3f6a70]        ; bt_av_sink_callbacks
cbz  x8, use_src
ldr  x8, [x8,#8]           ; ->connection_state_cb  (log "HAL bt_av_sink_callbacks->connection_state_cb")
... br  x8
use_src:
ldr  x8, [0x3f6a08]        ; bt_av_src_callbacks
ldr  x8, [x8,#8]           ; ->connection_state_cb
```
So: if sink callbacks are registered, every connection state goes to the sink JNI, whatever the peer. btif_av_is_sink_enabled() is just `[0x3f6a70] != 0`. btif_av_get_peer_sep() (0x1c1cc8) returns one global byte at 0x3f6de1, not per index. The per-index peer_sep exists (btif_av_cb[i] +0x12 in our earlier analysis; logged correctly above). btif_report_audio_state (0x1ba9dc) very likely has the same shape (not verified).

Questions:
- A1. Is the right v6 fix to make btif_report_connection_state / btif_report_audio_state (and every other report path that picks bt_av_sink_callbacks vs bt_av_src_callbacks) choose by the per-index role? They only receive a RawAddress: which QTI helper maps bd_addr -> btif_av_cb index on this build (symbol/offset), or should we read btif_av_cb[] directly (base address, element size, field offsets)?
- A2. CONNECTING is reported from the idle handler before SEP discovery, when peer_sep may still be unset. For outgoing connects the intent is known from the btif_queue request UUID (0x110A = we are source, 0x110B = we are sink); for incoming connects it is not. How do we carry the intended role into the slot before the first report, and what should incoming connections report until the SEP is known?
- A3. Please list every btif_av / btif_rc path on this build that dispatches by "which callbacks are registered" or by the global peer_sep instead of the slot role: connection state, audio state, sink audio_config_cb, source codec state, mandatory-codec query, active device, RC open/close (the XM6's RC went to the CT callbacks: your fix A in reverse). For each, the function and the decision point (address) you would patch.
- A4. Once the source link reaches CONNECTED, what still breaks with the single bta_av_co active peer: does SetActivePeer(car) for the split-A2DP encoder session (vendor.qti.hardware.bluetooth_audio 2.1, AFE proxy) stop or corrupt the sink decoder for index 0, and vice versa? Concretely, which call sites in bta_av_co must read a per-role active peer, and is a code-cave split (active_source_peer_ / active_sink_peer_) feasible in the existing BtaAvCo layout, or does it need more space than v5's cave approach allows?
- A5. maxconnectedaudiodevices sizes the shared btif_av pool (0x3dbf30). Is 2 right for 1 source + 1 sink, or should it be 3+ so a reconnecting phone never finds every slot taken? Why did slots stay stale after the Java side rejected the XM6 (index 1 stuck OPENED?), and should v6 issue a native disconnect when the Java service rejects a connection, or does BTA_AV_NUM_STRS / SCB allocation need changes too?
- A6. Should v6 ship as native-only (libbluetooth_qti.so) or does Bluetooth.apk need dex changes too (A2dpService / A2dpSinkService accounting, PhonePolicy auto-connect for a device that is a sink to one service and unknown to the other)?

## B. Open: the tablet cannot send AVRCP commands to the iPhone
Evidence: after a tablet-initiated connect, AvrcpControllerStateMachine for the iPhone goes Disconnected -> Connecting -> Connected ("Connection state 1->2"); AvrcpControllerService Devices Tracked = 1; RC events on index 0 have peer_sep 0. In this Bluetooth.apk, Connected.enter() (when mMostRecentState == CONNECTING) calls broadcastConnectionStateChanged(2) then BluetoothMediaBrowserService.addressedPlayerChanged(mSessionCallbacks), which does mSession.setCallback() when sBluetoothMediaBrowserService != null and otherwise logs "addressedPlayerChanged Unavailable" (never logged; the service answered onGetRoot a minute earlier). Yet the BT session stays `PlaybackState state=7 "Bluetooth audio disconnected", actions=0, active=false`, last updated 25 s after boot (BluetoothMediaBrowserService creation), and Play from a Media3 MediaBrowser (your dashboard) and from a framework MediaController on the MediaBrowser token (our launcher) never reaches the callback (its onPlay always logs "onPlay"; nothing). `cmd media_session dispatch play` reached the session as the media-button session the night before, also without effect.

Questions:
- B1. Your RC-routing hypothesis does not match (the CT connects). What else would leave the session with no live callback or ignore it: a second MediaSession instance (the token apps hold vs sBluetoothMediaBrowserService.mSession), the QTI fork of AvrcpControllerService/StateMachine gating callbacks on an "active device", or something in the v5 dex changes?
- B2. What instrumentation do you want? We can run any root shell command, set log properties, enable the HCI snoop log, and patch dex (we have tooling that patches Bluetooth.apk classes) or native code through Magisk modules.

## C. Strategy
- C1. Given A, is patching this QTI R stack to true concurrent source + sink realistic (how many patch sites, rough size), or would you recommend another route: a different stack/GSI (which Android/Fluoride version actually supports A2DP source and sink at the same time?), or hardware (an A2DP transmitter on the 3.5 mm jack for the car leg)?
- C2. If v6 is the route: can you write it as a patch spec against these exact binaries (function, address, original bytes, replacement, cave location), like v5? We will build, install and verify each step on the device and report logs.

## Data we can send
Full logcat of the XM6 test (carrelay_test/q1_iphone_connect_logcat.txt), stock and v5 libbluetooth_qti.so plus minidebug symbols (minidebug.elf, symtab_funcs.txt), stock and v5 Bluetooth.apk / dex dump, our disassembly notes (analysis/bluetooth/fable/dis_*.txt), dumpsys bluetooth_manager / media_session / audio_flinger snapshots, and an HCI snoop log of any test you specify.
