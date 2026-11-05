# Open problems

Evidence labels:
- **PROVEN:** seen directly in logs, packets or disassembly in this folder.
- **INFERRED:** follows from the evidence but was not tested.
- **UNKNOWN:** not explained.

The R numbers match `notes/FINDINGS.md`. Disassembly paths are under `firmware/disassembly/2026-11-04/`. Log timestamps use the tablet's wrong clock.

Every fix idea below is an untested suggestion. **None of these problems has been fixed.**

---

## Blockers for dual Bluetooth

### P1. The source link to a car never connects at the Java layer (main blocker)
**PROVEN.** Tested with a Sony WH-1000XM6 standing in for the car.

- **The native link is fine:** `btif_av` index 1 goes CONNECT_REQ -> SDP -> AVDT connect -> `STR_OPEN_OK` -> `BTA_AV_OPEN_EVT`, AAC 44.1 kHz, `peer_sep: 1`.
- **The state goes to the wrong service:** `BluetoothA2dpSinkServiceJni: bta2dp_connection_state_callback` -> `A2dpSinkService: Maximum number of A2DP Sink Connections reached: 1` -> `State Machine not found`.
- **Result:** `A2dpStateMachine` stays CONNECTING, times out after 30 s and loops. No Bluetooth A2DP output ever appears in audio policy.
- **With the sink slot free**, the headphones connect as an A2DP *sink device* plus AVRCP controller, which is the wrong role.
- **Cause:** `btif_report_connection_state` (0x1ba370) uses `bt_av_sink_callbacks` ([0x3f6a70]) whenever they are registered, otherwise `bt_av_src_callbacks` ([0x3f6a08]). There is no per-peer check, although the per-slot `peer_sep` is correct.

Files: `btif_report_connection_state.txt`, `logs/2026-11-04_carrelay/q1_iphone_connect_logcat.txt` (from 17:02:52), `notes/questions_for_uncle_v2.md` section A.

### P2. Other role-blind paths: extent not mapped
**PROVEN in part, UNKNOWN in extent.**

- `btif_av_get_peer_sep` (0x1c1cc8) returns one global byte (0x3f6de1), not a per-index value.
- `btif_av_is_sink_enabled` (0x1be6d4) only tests `[0x3f6a70] != 0`.
- `btif_report_audio_state` (0x1ba9dc) probably has the P1 shape. Its disassembly is included but has not been analysed.

**Needed from you:** a full list of decision points that dispatch on "which callbacks are registered" or on the global `peer_sep`.

### P3. One shared `btif_av` instance pool, role-blind connect
**From the 2026-11-03 binary analysis (`notes/ledger.md` 11:40). Not re-verified on 2026-11-04.**

- There is one pool ([0x3dbf30]), sized by source init.
- `REGISTER_EVT` assigns handles by arrival order, with no role.
- `connect_int` takes the first idle index regardless of UUID.
- v3 tried separate sink registrations and got `failed to alloc SCB` x5. With limit 1, the sink registrations got no btif index.
- v5 sidesteps this: the source registration also creates sink SEPs (codecs 7-9, tsep 1). That made receiving work but leaves the role problem.

The 2026-11-03 verdict was "a multi-function rework, not surgical patches". **Please challenge or confirm that.**

### P4. Single active peer in `bta_av_co`
**UNTESTED: nothing can reach this point until P1 is solved.**

- `bta_av_co_get_active_peer` (0x226124) and `bta_av_co_set_active_peer` (0x2262dc) assume one peer.
- During normal sink playback the log shows `peer index out of bounds: 5` and `No active peer codec config found, by default splitmode`, yet decoding runs.
- **Open question:** does `SetActivePeer(car)` for the split-A2DP encoder session (`vendor.qti.hardware.bluetooth_audio`, offload `sbc-aac`) stop or corrupt the sink decoder, and the reverse?

### P5. The three solutions have never had a car output
**PROVEN that they are blocked. Their behaviour once unblocked is UNKNOWN.**

| Solution | Mode | Idea | Status |
|---|---|---|---|
| `solution1-native-stack` | 1, native focus | The stack plays the sink stream as a media track; audio policy routes it to the active A2DP device | No active A2DP output exists |
| `solution2-oboe-bridge-app` | 2, Oboe duplex | Full-duplex stream, Bluetooth A2DP input -> A2DP output device | No output device |
| `solution3-audiopatch-bridge` | 3, AudioPatch | `createAudioPatch` from BT A2DP in to BT A2DP out | No output port |

The module's sequencer stops at step 1: `car A2DP source did not connect`. **The real car (Verano) has never been tested;** every dual test used the XM6.

### P6. Slots go stale after a failed source attempt
**PROVEN once, not retested.** After the XM6 test the iPhone's sink link would not reconnect (`connectA2dpNative` issued, no callback) until a Bluetooth restart. That was under limit 2. It has not been retested under stock 5.

---

## Dual-role side effects on the iPhone link

### P7 (R6a). Vendor commands carry the wrong company ID
**PROVEN.**

- `p_bta_av_cfg` (.bss 0x3def20) is one global, written only in `bta_av_api_register` at 0x1288a4: UUID 0x110B -> sink table 0x3d22f0 (company 0x001958, `AVRC_CO_METADATA`); 0x110A -> source table 0x3d2260 (company 0xDAA119, Google's ID).
- `BTA_AvVendorCmd` reads the ID through it (0x126eb0).
- **On the wire**, the tablet's controller sends `GetCapabilities` with `da a1 19`. Every other frame has `00 19 58`. So the source table is in effect.
- **The iPhone replies REJECTED, status 03**, within about 30-80 ms, on every connection.
- That the wrong ID is *why* it rejects is **INFERRED**.
- The tables also differ at +8 (CT/TG categories: sink `c1 03 / 02 00`, source `02 00 / d1 00`) and at +0x20 (01 vs 07). `bta_av_rc_create` and `bta_av_conn_chg` read this pointer too.

Files: `bta_av_cfg_tables.txt`, `BTA_AvVendorCmd.txt`, `bta_av_api_register_v5.txt`, `logs/2026-11-04_carrelay/q2_avctp_frames.txt`, `logs/2026-11-04_full_run/btsnoop_hci_filtered_sanitized.log`.

### P8 (R6b). Vendor replies go to the target handler, so the controller times out
**PROVEN from code and log timing.**

- In `btif_rc_handler`, `BTA_AV_META_MSG_EVT` (0x20cdc0) tests the target callbacks first: `ldr x8,[0x4003e8]; cbz x8,0x20cfe4`, then tail-calls `handle_rc_metamsg_cmd`.
- The controller branch (0x20cfe4 -> `handle_avk_rc_metamsg_rsp` 0x20d1d0) only runs when target callbacks are NULL. In dual role the target side (`Avrcp_ext`, from `A2dpService`) is up.
- **Log:** `avrc_pars_vendor_cmd: length is incorrect, status: 3`, then `handle_get_capability_response: Error capability response: 0xFE` exactly 2 s after connect, on every connection.
- Reading 0xFE as btif_rc's status-command timeout comes from memory of AOSP (`BTIF_RC_STS_TIMEOUT`). The 2 s timing fits. Please verify.

**Effect of P7 + P8:** no event registration, so the session stays `state=7 "Bluetooth audio disconnected"`, `metadata: null`, `actions=0`. Car Home shows "Nothing playing" during real playback and "does not share its library".

**INFERRED:** both must be fixed together. The 2026-11-03 notes record the library working in receive-only mode, when the source side is not running. The company-ID value and dispatch path in that mode were not captured.

### P9 (R7). No AVRCP browsing channel
**PROVEN that it is absent. Cause UNKNOWN.** No L2CAP connect for PSM 0x1B from either side; `conn state: rc: 1 br: 0`. It may be the source table's CT categories (P7). That is not proven.

### P10 (R8). iPhone volume does not follow the tablet
**PROVEN.**

- The iPhone asks `GetCapabilities(EVENTS)`. The tablet's target side answers `01 02 05 08 0a 0b`, with no 0x0d `VOLUME_CHANGED`. Same dispatch as P8.
- The target also sends `RegisterNotification(VOLUME_CHANGED)` to the iPhone at connect; the iPhone answers NOT_IMPLEMENTED.

### P11. Pause does not work from the tablet
**PROVEN.**

- Media-key PAUSE reaches the session and is dropped, because `actions=0`.
- Car Home's button stays on Play because the session never reports playing, so it never sends `pause()`.
- **There is currently no way to stop music from the tablet** except dropping the link. Passthrough itself works (PLAY key 68, NEXT key 75).
- A launcher workaround is possible, but the root cause is P7/P8.

---

## Home-screen player card (reported by the owner)

The owner reports three things about the launcher's player card: it does not show accurately in real time what song is playing, its buttons struggle to control the phone over Bluetooth, and there is no cover art. Code: `source/carhome/.../Home.kt` (`MusicCard`, `showMusic`), `MusicUi.kt` (`NowPlayingPanel`), `CarState.kt` (`pickSession`, `publishMedia`, `playPause`).

### P18 (R13). The card does not show the current song in real time
**Symptom reported by the owner. PROVEN in dual role that no track data reaches the launcher.**

- During real playback the Bluetooth session stays `metadata: null`, `state=7`, `updated=<25 s after Bluetooth start>` (`logs/2026-11-04_full_run/dumpsys_media_session_2_playing_from_tablet.txt`). Car Home has nothing to show, and the Music screen says "Nothing playing".
- `showMusic` in `Home.kt` only turns on when `media.playing` is true, which this session never reports. So **during playback the home screen keeps showing the Sound card instead of the player card.**
- `pickSession` also ignores an inactive Bluetooth session unless it has metadata and a playing or paused state.
- **INFERRED:** the cause is P7 + P8, since without event registration no track or play-status updates arrive.
- **UNKNOWN:** whether the card is also inaccurate or slow in receive-only mode, where metadata should flow. The 2026-11-03 notes record the library working there, but card accuracy was not measured. Please separate launcher logic from stack behaviour here.

### P19 (R13). The buttons struggle to control the phone
**Symptom reported by the owner. Partly measured.**

| Button | Result on 2026-11-04 | Evidence |
|---|---|---|
| Play | Reaches the iPhone. Press to ACCEPTED took 0.61-0.95 s across 4 presses, and the stream starts at about the same moment | `q2_avctp_frames.txt`, `btsnoop_hci_filtered_sanitized.log` |
| Next | Reaches the iPhone. ACCEPTED in about 0.035 s while playing | `q2_resume_logcat_20261104.txt`, 09:37:54 |
| Previous | Not measured | |
| Pause | **Cannot be sent:** the button stays on Play (P11) | `CarState.playPause` |

- With no playing state, the shared button always sends PLAY. Pressing it during playback does nothing useful, which may be what "struggle" looks like to the driver.
- The whole failure on the night of 2026-11-03/04 (P16) is also unexplained, so intermittent failures cannot be ruled out from 4 successes.
- **Please check** the press/release timing (7-9 ms apart between the two passthrough frames in the snoop) and whether the launcher should have any control path that does not depend on session state.

### P20 (R13). No cover art
**Symptom reported by the owner. Cause not established.**

- The 2026-11-03 notes say the Bluetooth app's AVRCP cover-art (BIP) support is off, with no cover-art provider. **Not re-verified.** `firmware/stock/resources.txt` holds `avrcp_cover_art_default_*` values, and the classes are in `classes_dump.txt.zip`.
- Even with BIP on, the image handle arrives with track metadata, so P7/P8 would likely block it in dual role (**INFERRED**).
- The iPhone reports `AvrcpTgVersion = 262` (0x106, AVRCP 1.6) in `bt_config_redacted.conf`. **Whether it actually offers cover art over Bluetooth is UNKNOWN.** Cover-art support is advertised inside the AVRCP SDP record, so the service UUID list alone does not settle it.
- `Home.kt`'s `Cover` and `CarState.publishMedia` read `METADATA_KEY_ALBUM_ART` / `METADATA_KEY_ART` if they are present. The launcher side has never received any, so it is untested.

---

## Stability

### P12 (R9). Native Bluetooth abort when disabled during streaming
**PROVEN, seen once.**

`svc bluetooth disable` while the sink was streaming: SIGABRT in thread `btu message loop`.

    #00 abort
    #01 libbluetooth_qti.so +0x1bf1ec  bte_av_sink_media_callback
    #02 +0x11c824  bta_av_sink_data_cback
    #03 +0x279030  avdt_scb_event
    #04 +0x27060c  avdt_ad_tc_data_ind
    #05 +0x308934  l2c_csm_execute

- **File:** `logs/2026-11-04_full_run/crash_buffer_bluetooth_abort.txt`, 16:46:15.
- `bta_av_sink_data_cback` is a callback v5 wires into the source registration, so **v5 may be involved. Unverified.**
- Three earlier restarts during playback that day were not checked for this crash, so the rate is unknown.

### P13 (R10). The stream resumes after that crash
**PROVEN.** Bluetooth came back with a new PID, the iPhone reconnected, and the music played again with no pause available. Car Home's Disconnect stopped it.

### P14 (R11). The iPhone did not come back after Disconnect then Connect
**UNKNOWN.** On Car Home 1.7's new Bluetooth screen, no A2DP sink state machine had appeared after about 9 s. The tap, the timing or stale state are all possible. Not investigated, at the owner's request.

### P15 (R12). SystemUI crash
**PROVEN that it happened. Link to Bluetooth UNKNOWN.** `java.lang.RuntimeException: Cannot initialize effect engine`, 16:47:49, about 1.5 min after the abort and close to the Disconnect/Connect taps. The logcat capture had stopped before this, so only the `am_crash` event and the crash buffer were saved. The crash buffer may hold its stack; check `crash_buffer_bluetooth_abort.txt`.

---

## Unexplained

### P16 (R3). Why tablet Play failed on the night of 2026-11-03/04
**UNKNOWN.**

- That night, Play from Car Home never reached `onPlay`, and from the module's Media3 dashboard it was dropped client-side (actions=0). The capture has no `onPlay` line.
- On 2026-11-04 evening it worked 4 of 4: session active; after a Bluetooth restart with the session inactive; with `maxconnectedaudiodevices=2` restored; and after a clean boot.
- So removing that override in v1.0.2 is ruled out as the fix.
- `active=true` follows the stream starting (`requestMediaKeyFocus`); it did not cause the Play to work.
- Tonight's session dump shows one Bluetooth session. `dumpsys` does not show callbacks, but `onPlay` firing shows one is set.

### P17. Log noise with no visible effect
**UNKNOWN.** `bta_av_ssm_execute(): AV Sevent(0x41)=0x121b(STR_OPEN_FAIL) state=3(OPEN)` about 2 s after connect; `handle_get_capability_response` 0xFE (P8); `peer index out of bounds: 5`.

---

## Already closed: no need to revisit

- **`carrelay_ivi` review items** (first-boot Bluetooth kill, persistent app, global hidden-API policy, libsu, overlay log messages): fixed by v1.0.1, verified.
- **Native sink worker not started in dual role** (`fixed_queue` abort): fixed by v1.
- **3 s auto-suspend and wrong remote-start path:** fixed by v2.
- **Sink SEPs not created:** fixed by v5.
- **LSPosed approach:** abandoned, see constraints in `00_START_HERE.md`.

## Separate work: not part of this audit

Call audio through the tablet (`hfp_audio`, `firmware/audio/`) still needs a real call test. The launcher's compass and Bluetooth screen do not touch the stack.
