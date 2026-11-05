# Stack notes — device-verified state of the QTI R stack + btdual_patch v5

Device: TB-J606F / S320311_230810_ROW, Magisk, `btdual_patch v5`, CarRelay v1.0.1.
Everything below is from the tablet unless marked "open".

## 1. Phone → tablet (A2DP sink) — works

* With `CarIvIBtOverlay` (`a2dp_sink_automatically_request_audio_focus=true`):
  `shouldRequestFocus=true`, focus granted, the uid‑1002 sink AudioTrack plays at
  44.1 kHz with 0 underruns as soon as the iPhone presses Play.
* Decoding starts **without** an active sink peer. `bta_av_co_get_active_peer: peer
  index out of bounds: 5` comes from Avrcp_ext (TG) polling, appears even with only
  HFP connected, and is noise. The earlier assumption that the decoder needs
  `BtifAvSink::SetActivePeer` was wrong for this firmware.
* The AVRCP Controller state machine connects for the iPhone (`Devices Tracked 1`,
  Connected); RC events arrive with `peer_sep 0` on the CT callbacks.
  `Connected.enter()` calls `addressedPlayerChanged → setCallback`; the browser
  service is alive.

**Open:** the tablet cannot start iPhone playback. The
`BluetoothMediaBrowserService` session stays `STATE_ERROR (7)`, `active=false`,
`actions=0`, and `play()` from a Media3 controller and from a framework
`MediaController` never reaches `onPlay`.

Two things to check next, in this order:

1. `adb shell dumpsys media_session` — find the `com.android.bluetooth` sessions.
   Confirm which session the browser token refers to, whether it has a callback
   registered, and its `active` flag. If the token points at a session with no
   callback while another Bluetooth session (Avrcp_ext target) has one, the
   firmware is handing the browser a different session than the one the CT state
   machine configured.
2. `setprop log.tag.AvrcpControllerStateMachine DEBUG`,
   `setprop log.tag.BluetoothMediaBrowserService DEBUG`, then press Play from a
   framework `MediaController` and watch `logcat -s AvrcpControllerStateMachine
   BluetoothMediaBrowserService MediaSessionRecord`. If `MediaSessionRecord`
   dispatches the play but no `onPlay` is logged, the callback handler is not
   running (its Looper is gone); if the dispatch itself is missing, the controller
   is bound to the wrong session.

Note for the Media3 path specifically: Media3 derives available commands from
the legacy `PlaybackStateCompat.actions`; with `actions=0` it drops `play()`
client-side. That alone does not explain the framework-controller failure, so the
checks above are the ones that matter.

## 2. Tablet → car (A2DP source) while sink is enabled — fails natively

Tested with WH-1000XM6 headphones standing in for the car:

* The source link opens natively: `btif_av` index 1, `STR_OPEN_OK`,
  `BTA_AV_OPEN_EVT`, AAC 44.1 kHz.
* Its connection state is reported through **`BluetoothA2dpSinkServiceJni`**
  (`bta2dp_connection_state_callback`), so `A2dpSinkService` sees it
  ("Maximum number of A2DP Sink Connections reached: 1") and `A2dpService`
  never receives CONNECTED; it times out every 30 s.
* With the sink slot free, the headphones connect as an A2DP **sink device +
  AVRCP CT** (wrong role).
* The sequencer therefore stops at step 1 ("car A2DP source did not connect");
  every callback for the headphones goes through the sink JNI.
* Afterwards the iPhone's sink link would not reconnect until a Bluetooth
  restart (stale `btif_av` slots). `persist.bluetooth.maxconnectedaudiodevices`
  is no longer overridden by the module (stock 5) to keep head-room, but the
  slot leak itself is native.

**Requirement for btdual_patch v6:** role-aware connection-state reporting in
`btif_av` — deliver `BTA_AV_OPEN_EVT`/`CLOSE_EVT` to the **source** callbacks
when the local SEP is SRC or the peer is a Sink, and to the sink callbacks only
when the local SEP is SNK, instead of choosing by the global sink flag. Slot
allocation/`connect_int` and the `bta_av_co` active peer should follow the same
per-role split. Until then none of Modes 1/2/3 can obtain a car output; the
routing layer is not the limiting factor.

## 3. What the app layer can and cannot do on SDK 30

* No binder path exists to set the A2DP sink active device
  (`BluetoothAdapter.setActiveDevice` is S+); not needed on this firmware anyway.
* AVRCP pass-through to the phone is reachable only through the browser
  session (`BluetoothAvrcpController` exposes group navigation only).
* Mode 1 now reports "AVRCP browser session in error state" instead of
  pretending `prepare()`/`play()` were delivered.
