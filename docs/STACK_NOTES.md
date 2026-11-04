# Stack notes — why the sink peer never becomes active, and what "both roles at once" needs

Context: CarRelay v1.0.0 on TB-J606F / S320311_230810_ROW with `btdual_patch v5`.
Symptom: `BluetoothMediaBrowserService`'s session sits in `STATE_ERROR (7)`
"Bluetooth audio disconnected" from boot; `prepare()`/`play()` do nothing; logcat shows
`bta_av_co_get_active_peer: peer index out of bounds: 5`.

## Q1 — how the sink-side peer becomes active on the R stack

### What the two log lines mean

* **`peer index out of bounds: 5`** — `BtaAvCo::GetActivePeer()` returns the peer at
  `active_peer_index`; the sentinel "no active peer" is `BTA_AV_NUM_STRS` (= 5 on this
  build, hence index 5). So the message is literally *"someone asked for the active A2DP
  peer and none has been set"*. Every sink-side data-path call goes through it:
  `GetSinkDecoderInterface()` → `GetActivePeer()`, used by `btif_a2dp_sink_on_start()`,
  `btif_a2dp_sink_audio_handle_start_decoding()` and the sink `set_focus_state` path.
  No active peer ⇒ no decoder interface ⇒ **decoding never starts, silence**, even though
  the ACL/AVDTP link is up and `A2dpSinkService` reports CONNECTED.
* **Session `STATE_ERROR` "Bluetooth audio disconnected"** — that is the state
  `BluetoothMediaBrowserService.onCreate()` installs. It is replaced only when an
  `AvrcpControllerStateMachine` reaches `Connected` and starts pushing playback state /
  metadata for its device. Stuck at 7 since boot ⇒ **the AVRCP Controller state machine
  never connected for the iPhone**, whatever `BluetoothAvrcpController.getConnectionState`
  says. With no connected CT state machine the session has no live callback, so
  `prepare()`/`play()` from Media3 are swallowed.

### Who sets the sink active peer in AOSP R

`btif_av.cc`, `BtifAvStateMachine::StateOpened::OnEnter()`:

```cpp
  // Set the active peer if the first connected device.
  // NOTE: This should be done only if we are A2DP Sink, because the A2DP Sink
  // implementation in Java expects that the active peer will be set only by
  // the Java framework (via the a2dp set_active_device JNI call).
  if (peer_.IsSink() && btif_av_source.ActivePeer().IsEmpty()) {
      btif_av_source.SetActivePeer(peer_.PeerAddress(), ...);
  }
```

`peer_.IsSink()` = *the remote is a Sink* (we are Source → car link). For a remote
**Source** (iPhone) the native stack deliberately does **nothing**; the Java side must
call `A2dpSinkService.setActiveDevice()` → `setActiveDeviceNative()` →
`BtifAvSink::SetActivePeer()` → `bta_av_co_set_active_peer()`.

In R the **only** in-tree caller of `A2dpSinkService.setActiveDevice()` is:

```java
// A2dpSinkService.java (R)
public boolean requestAudioFocus(BluetoothDevice device, boolean request) {
    ...
    if (!setActiveDevice(device)) { Log.w(TAG, "Failed to set " + device + " as active device"); return false; }
    mA2dpSinkStreamHandler.requestAudioFocus(request);
    return true;
}
```

and `requestAudioFocus(device, true)` is called from
`AvrcpControllerStateMachine.mSessionCallbacks.onPrepare()/onPlay()` — i.e. **from the
MediaSession of a *connected* AVRCP Controller state machine**. `IBluetoothA2dpSink` has
no `setActiveDevice` on R (that arrived with `BluetoothAdapter.setActiveDevice()` in S), so
there is **no binder path** an app can use to set the sink peer active.

The overlay's `a2dp_sink_automatically_request_audio_focus=true` path
(`A2dpSinkStreamHandler.SRC_STR_START → requestAudioFocusIfNone() →
startFluorideStreaming() → informAudioFocusState()`) does **not** go through
`A2dpSinkService.requestAudioFocus(device, …)` and therefore never sets the active peer —
it only produces the `peer index out of bounds` line when the native focus handler asks
for the decoder. So on this stack the overlay bit is necessary for *keeping* focus but is
not what starts audio.

### Root cause hypothesis (btdual_patch v5)

AOSP R keeps two independent peer pools (`BtifAvSource::peers_`, `BtifAvSink::peers_`)
and classifies each new `BTA_AV_OPEN_EVT` / `BTA_AV_RC_OPEN_EVT` by the role the stream
handle was registered with. The QTI R stack folds that into a single `btif_av_cb[]` with a
`peer_sep` field, and v5 then "gives every source-registered stream slot the sink SEPs".
Consequence: the iPhone's AVDTP connection lands on a slot whose *registration role* is
Source even though the negotiated SEP role is Sink. Two things follow:

1. `btif_rc` dispatches `BTA_AV_RC_OPEN_EVT` by that slot role. For a "source" slot it
   calls the **Target** callbacks (`bt_rc_callbacks`, → `AvrcpTargetService`, disabled by
   resource gate → dropped) instead of the **Controller** callbacks
   (`bt_rc_ctrl_callbacks` → `AvrcpControllerService.onConnectionStateChanged`). Hence no
   `AvrcpControllerStateMachine` for the iPhone, session stuck in `STATE_ERROR`, no
   `onPrepare()`, no `setActiveDevice()`, no decoder.
2. Even if (1) were fixed, `bta_av_co` has a **single** `active_peer_` shared by both
   roles (true in AOSP R as well). Setting the iPhone active for the sink decoder
   clobbers the car as the source encoder's peer (see Q2).

Verification (no code change): `logcat -b main -s bt_btif_rc:V bt_btif_av:V bt_bta_av:V`
while the iPhone connects. Look for `handle_rc_connect`, which callback set it chose
(`rc_features`, "RC connect ... as CT/TG"), and whether `btif_rc`'s
`connection_state_cb(true, …)` is delivered to the ctrl callbacks. If the TG path is taken
the diagnosis above is confirmed. Also `dumpsys bluetooth_manager` → `Profile:
AvrcpControllerService / Devices Tracked = 0` while the iPhone is connected is the same
signature.

### Fix options, in order of preference

| # | Where | Change | Effect |
|---|---|---|---|
| A | `libbluetooth_qti.so` (`btif_av` / `btif_rc`) | Classify the peer by the **negotiated SEP role** (`peer_sep == AVDT_TSEP_SRC` ⇒ we are Sink) when routing `BTA_AV_RC_OPEN_EVT`/`BTA_AV_OPEN_EVT`, not by the slot's registration role. | AVRCP CT connects → session leaves error → `prepare()`/`play()` work → `requestAudioFocus()` sets the sink active peer → decoder starts. Also restores metadata/controls (needed by the Now Playing bar). |
| B | `libbluetooth_qti.so` (`btif_av`, `StateOpened::OnEnter`) | Mirror AOSP's source logic for the sink: `if (peer is Source && sink active peer empty) btif_av_sink.SetActivePeer(peer)`. ~10 instructions. | Audio flows with the overlay's auto-focus alone, independent of AVRCP. Does **not** fix AVRCP controls/metadata. Must be combined with the per-role active peer from Q2 or it steals the car's encoder peer. |
| C | Java (`A2dpSinkService.java` in Bluetooth.apk) | Call `setActiveDevice(device)` in `A2dpSinkStateMachine.Connected.enter()` or in `A2dpSinkStreamHandler.SRC_STR_START`. | Same effect as B but in dex; easier to patch with the existing two-dex-branch tooling of v5. Same Q2 caveat. |
| D | App side | Nothing available on R: no binder `setActiveDevice` for A2DP sink, no shell command. The dashboard can only *detect* the condition (AVRCP CT disconnected while A2DP sink connected → "AVRCP controller not bound to iPhone — native rc role dispatch" hint). | — |

Recommendation: A (+ the Q2 split) in btdual_patch v6. B/C are acceptable stop-gaps to
prove the audio path while A is worked on.

## Q2 — simultaneous Source (car) + Sink (iPhone)

You are right that this lives in the native stack. Concretely what breaks:

* **`bta_av_co` single active peer.** Both `GetSourceEncoderInterface()`/
  `audio_src_data_path()` (split-A2DP encoder session for the car via
  `vendor.qti.hardware.bluetooth_audio`) and `GetSinkDecoderInterface()`/sink data path
  resolve through the one `active_peer_`. Whoever calls `SetActivePeer()` last wins;
  the other role either stops (`no active peer`) or, worse, reads the wrong peer's codec
  config (`bta_av_co_get_peer_params`) and reconfigures the encoder with the sink peer's
  SEP. Expected signature when the iPhone activates after the car: car audio stops /
  codec re-negotiation storm on the car link within ~1 s.
* **Role-blind handle assignment / `connect_int`.** `BTA_AV_NUM_STRS = 5`; with v5 every
  slot carries both SEPs, so an incoming connection from the car (Sink role remote) may be
  accepted on a slot that is already the iPhone's or vice versa, and `connect_int` picks
  the first free handle without checking the role the peer needs. Pairing the two
  connections in a deterministic order (car first, as the sequencer does) reduces but does
  not remove the race, because the iPhone reconnects on its own schedule.
* **`btif_a2dp_source` / `btif_a2dp_sink` worker threads** are already separate, and the
  audio HAL side is fine: the source is offloaded (ADSP encoder, AFE proxy) while the sink
  decodes in software and plays through an AudioTrack. No conflict there.
* **Controller**: two ACL links with sniff on WCN3980 are routine (HFP+A2DP do it). Keep
  the car link on SBC/AAC (module `system.prop`) so the combined air time stays safe.

What v6 needs for concurrency:

1. `BtaAvCo`: split `active_peer_` into `active_source_peer_` and `active_sink_peer_`;
   make `GetActivePeer()` role-aware at every call site (encoder/source paths → source
   peer; decoder/sink paths → sink peer; `SetActivePeer(addr)` chooses the field from the
   peer's `peer_sep`).
2. `btif_av`: keep the dual registration (that is what v5 achieved) but make slot
   allocation and `connect_int` role-aware — a slot already bound to a Source-role peer
   must not be handed to a Sink-role connection, and vice versa; `BTIF_AV_SET_ACTIVE_DEVICE_REQ`
   must target the matching role's active peer.
3. `btif_rc`: route `BTA_AV_RC_OPEN_EVT` by the peer's negotiated SEP (this is fix A).

Until that exists, treat the three routing modes as **decoder-side experiments only**:
Mode 1/2/3 all assume the stack can hold both links. The test that settles Q2 is: car
connected and streaming (`dumpsys bluetooth_manager` → `A2dpService mActiveDevice = car`,
A2DP State TxQueue counters moving), then connect the iPhone and start playback; if the
TxQueue counters freeze or `bta_av_co_set_active_peer` flips to the iPhone in logcat, it is
the single-active-peer problem and no routing choice on top will fix it.
