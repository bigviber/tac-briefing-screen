# v6 static defect review — dual A2DP (source+sink) + AVRCP + launcher

Static review only. Based on the plain-text disassembly in `firmware/disassembly/2026-11-03/` and
`firmware/disassembly/2026-11-04/`, the config dumps in `bta_av_cfg_tables.txt`, `01_PROBLEMS.md`,
`02_TIMELINE.md`, and the launcher sources. No binaries were opened and no binary-patching script was run
or written. Native fixes are given as AOSP C reference + a **Static ARM64 Instruction Verification Table**
keyed to the exact virtual addresses in the listings. Addresses are `libbluetooth_qti.so` vaddrs
(`.text` offset == vaddr, per `patch_diff_stock_vs_v5.txt`).

Nothing below has been built, flashed, or run. Every "replacement" is a proposal to be assembled and
verified on-device, exactly like the v5 patch spec.

---

## 0. Ground truth re-established from the listings

### 0.1 The `btif_av_cb[]` pool (per-peer slot layout)

Two different addresses get called "the pool"; they are not the same thing:

| Symbol in prompt | What it actually is | Evidence |
|---|---|---|
| `0x3dbf30` | **scalar** `btif_max_av_clients` / active-index **count** (sized by `maxconnectedaudiodevices`) | `connect_int` `ldr w8,[x25,#0xf30]` with `x25=0x3db000`, used as the loop bound; `02_TIMELINE`/questions A5 |
| `0x3f6600` | **base of the `btif_av_cb[]` array**, element stride `0xc0` (192 B) | `connect_int` `adrp x24,#0x3f6000; add x24,x24,#0x600`; stride `add x21,x21,#0xc0` |

Per-slot field offsets, recovered from `connect_int` (2026-11-03) and the OPEN path in
`btif_av_state_opening_handler` (2026-11-03 `dis_opening.txt`):

| Slot offset | Field | Evidence |
|---|---|---|
| `+0x00` | `flags` (byte) | `started_handler` `ldrb/orr #0x10/strb [slot]` |
| `+0x01 .. +0x06` | `peer_bda` (RawAddress, 6 B) | `connect_int` `ldur w9,[x27,#1]; ldurh w11,[x27,#5]` compared to the passed `bd_addr` |
| `+0x08` | `sm_handle` (`btif_sm_handle_t`, 8 B ptr) | `connect_int` `ldr x0,[x27,#8]; bl btif_sm_get_state` |
| `+0x11` | `edr` | `opening` `ldrb w8,[p_data,#0xa]; strb w8,[slot,#0x11]` |
| **`+0x12`** | **`peer_sep`** (`AVDT_TSEP`: 0=SRC, 1=SNK) | `opening` `ldrb w8,[p_data,#0xb]; strb w8,[slot,#0x12]`, then read back `ldrb w8,[slot,#0x12]; cbz(==0 SRC)…; cmp #1(==1 SNK)` at `0x1b4f40`. Independently confirmed in `notes/questions_for_uncle_v2.md` §A ("`btif_av_cb[i] +0x12`"). |
| `+0x30` | 3-Mbps/aptX flag | `opening` `strb w9,[slot,#0x30]` |
| `+0x3c` | starting/reconfig flag | `opening` `strb #1,[slot,#0x3c]` when `p_data+0xc==1` |
| `+0x46` | `tws_device` | `opening` `ldrb w8,[p_data,#0xd]; strb w8,[slot,#0x46]` |
| `+0x52` | `avdt_sync` | `opening` `strb w9,[slot,#0x52]` |
| `+0x56` | aptX mode (halfword) | `opening` `ldrh/strh [slot,#0x56]` |

The OPEN handler logs confirm the roles on this exact build: iPhone slot = `peer_sep: 0, index: 0`;
XM6/car slot = `peer_sep: 1, index: 1` (questions §A, point 1).

### 0.2 AVDT_TSEP semantics and the dispatch each role needs

`peer_sep` is the **remote** peer's SEP type.

| `peer_sep` | Remote device | Tablet's A2DP role | Correct JNI callback table | Java service |
|---|---|---|---|---|
| `0` = `AVDT_TSEP_SRC` | iPhone (sends audio) | Sink | `bt_av_sink_callbacks` `[0x3f6a70]` | `A2dpSinkService` |
| `1` = `AVDT_TSEP_SNK` | Car / XM6 (receives audio) | Source | `bt_av_src_callbacks` `[0x3f6a08]` | `A2dpService` |

### 0.3 Helpers available for a per-peer fix (from `symtab_funcs.txt`)

- `btif_av_idx_by_bdaddr(RawAddress*)` → `0x1af7fc` — returns the `btif_av_cb[]` index for an address, or
  `btif_max_av_clients` (`[0x3dbf30]`) when not found. This is the `bd_addr → index` map questions §A1 asks for.
- `btif_av_get_addr_by_index(int)` → `0x1be408`.
- `btif_av_get_peer_sep()` → `0x1c1cc8` — **global** byte `[0x3f6de1]` (last OPEN wins). This is the P2 hazard.

---

## Deliverable 1 — P1/P2: Java-layer vs native-layer dispatch fix

### 1.1 Why the Car's link lands in the Sink service (root cause, PROVEN)

`btif_report_connection_state(btav_connection_state_t state, RawAddress* bd_addr)` @ `0x1ba370` dispatches
purely on *which callback table is registered*, never on the peer:

```c
// stock / v5 — effective logic of 0x1ba370
static void btif_report_connection_state(btav_connection_state_t state, RawAddress* bd_addr) {
  if (bt_av_sink_callbacks /*[0x3f6a70]*/) {                 // 0x1ba388 ldr; 0x1ba394 cbz
    bt_av_sink_callbacks->connection_state_cb(state, bd_addr); // +8 in the table; 0x1ba46c ldr x1,[x8,#8]
  } else if (bt_av_src_callbacks /*[0x3f6a08]*/) {           // 0x1ba3e0
    bt_av_src_callbacks->connection_state_cb(state, bd_addr);
  } else { /* ASSERT "Callback is NULL" */ }
}
```

In v5 the Java layer force-adds **both** profiles (`Config.addAudioProfiles` gate bypass, see
`02_TIMELINE` "How v5 differs"), so **both** tables are non-NULL and the sink branch always wins. Every
peer — including the Car (`peer_sep==1`) — is routed to `BluetoothA2dpSinkServiceJni`, which answers
`A2dpSinkService: Maximum number of A2DP Sink Connections reached: 1` → `State Machine not found` →
`A2dpStateMachine` loops in CONNECTING and no A2DP-source output device is ever created.

`btif_report_audio_state` @ `0x1ba9dc` has the **identical** shape (decision `cbz x8,#0x1baa48` at
`0x1baa00`; audio cb at table `+0x10`). `btif_av_is_sink_enabled` @ `0x1be6d4` is just `[0x3f6a70]!=0`,
and `btif_av_get_peer_sep` @ `0x1c1cc8` returns the single global byte `[0x3f6de1]` — neither is per-peer.
That is the full extent P2 asked to map for the connection/audio report paths; the RC paths are in
Deliverable 2, and the sink-decode global-sep hazard is in Deliverable 3.

### 1.2 Path 1A (Java JNI router in `Bluetooth.apk`) — evaluation

**Verdict: not sufficient on its own; rejected as the primary fix.**

- `BluetoothA2dpSinkServiceJni.bta2dp_connection_state_callback`/`bta2dp_audio_state_callback` only ever
  receive the Car's events because the native dispatch already picked the **sink** table (1.1). A router in
  `A2dpSinkService`/`AdapterService` that forwards a `StackEvent` to `A2dpStateMachine` when
  `A2dpService.getConnectionPolicy(device)==CONNECTION_POLICY_ALLOWED` (or
  `A2dpSinkService.getConnectionPolicy(device)==FORBIDDEN`) **can** get `A2dpStateMachine` to `Connected`.
- **But streaming still breaks in native.** Once `A2dpStateMachine` is `Connected`, the source data path
  calls back into `btif_av`. `btif_av_get_peer_sep()` (`0x1c1cc8`) is the **global** last-OPEN byte, and the
  sink-decode path gates on it (`btif_a2dp_sink_handle_inc_media` `0x1a5864`: `bl get_peer_sep; cmp #1;
  b.eq skip`). So the Java router does not stop native from mis-keying source vs sink; it just moves the
  failure. Audio-state, codec-config, active-device and the "stale slot after reject" (P6) paths are all
  still native-role-blind.
- Net: a Java router is at best a cosmetic state-machine fix layered on a native stack that still cannot
  tell the two links apart. The honest fix is native (Path 1B). A thin Java change is still wanted for
  **accounting** (so `A2dpSinkService` does not count the Car against its max-1), but it is secondary.

### 1.3 Path 1B (native per-peer dispatch) — the fix

Make both report functions choose the table by the **slot `peer_sep`** for the reported `bd_addr`:

```c
// v6 btif_report_connection_state (same transform for btif_report_audio_state, cb offset +0x10)
static void btif_report_connection_state(btav_connection_state_t state, RawAddress* bd_addr) {
  uint8_t idx = btif_av_idx_by_bdaddr(bd_addr);        // 0x1af7fc
  uint8_t sep = (idx < btif_max_av_clients /*[0x3dbf30]*/)
                  ? btif_av_cb[idx].peer_sep           // base 0x3f6600 + idx*0xc0 + 0x12
                  : 0xFF;                              // not found -> keep stock behaviour
  btav_callbacks_t* cb;
  if      (sep == AVDT_TSEP_SNK /*1*/) cb = bt_av_src_callbacks;   // car is a sink  -> A2dpService
  else if (sep == AVDT_TSEP_SRC /*0*/) cb = bt_av_sink_callbacks;  // phone is a src -> A2dpSinkService
  else    cb = bt_av_sink_callbacks ? bt_av_sink_callbacks : bt_av_src_callbacks;  // unknown -> stock
  if (cb && cb->connection_state_cb) cb->connection_state_cb(state, bd_addr);
}
```

The computation does not fit inline, so (exactly like v5) redirect the single decision instruction to a
code cave. Registers `x19=bd_addr`, `w20=state`, `x21=0x3f6000` are already live at the hook;
`x19/x20/x21` are callee-saved across the `bl` and the cave preserves `x18` (it issues no shadow-stack
op of its own).

#### Static ARM64 Instruction Table — P1 `btif_report_connection_state` (0x1ba370)

| Virtual addr | Original instruction | Original bytes (LE) | Proposed replacement |
|---|---|---|---|
| `0x1ba394` | `cbz x8, #0x1ba3dc` | `48 02 00 b4` | `b CAVE_P1_CONN` |

`CAVE_P1_CONN` (new `.text` padding; back-branch offsets fixed at assembly time):

```asm
CAVE_P1_CONN:
    stp  x0, x1, [sp, #-16]!
    mov  x0, x19                    ; bd_addr
    bl   #0x1af7fc                  ; btif_av_idx_by_bdaddr -> w0 = idx
    adrp x9, #0x3db000
    ldr  w9, [x9, #0xf30]           ; btif_max_av_clients  (0x3dbf30)
    mov  w11, #0xff                 ; sep = UNKNOWN
    cmp  w0, w9
    b.hs 1f                         ; idx not found -> UNKNOWN
    mov  w9, #0xc0
    adrp x10, #0x3f6000
    add  x10, x10, #0x600           ; &btif_av_cb[0]  (0x3f6600)
    madd x10, x0, x9, x10           ; &btif_av_cb[idx]
    ldrb w11, [x10, #0x12]          ; peer_sep
1:  ldp  x0, x1, [sp], #16
    adrp x21, #0x3f6000             ; restore base the function relies on
    cmp  w11, #1                    ; AVDT_TSEP_SNK -> Source service
    b.eq 3f
    cbnz w11, 4f                    ; not 0 and not 1 -> UNKNOWN -> stock
    ldr  x8, [x21, #0xa70]          ; sep==0: sink path expects x8 = bt_av_sink_callbacks
    b    #0x1ba398
3:  b    #0x1ba3dc                  ; src path (reloads [0xa08] itself)
4:  ldr  x8, [x21, #0xa70]          ; stock: reproduce 0x1ba394 semantics
    cbz  x8, #0x1ba3dc
    b    #0x1ba398
```

#### Static ARM64 Instruction Table — P2 `btif_report_audio_state` (0x1ba9dc)

| Virtual addr | Original instruction | Original bytes (LE) | Proposed replacement |
|---|---|---|---|
| `0x1baa00` | `cbz x8, #0x1baa48` | `48 02 00 b4` | `b CAVE_P2_AUDIO` |

`CAVE_P2_AUDIO` is identical to `CAVE_P1_CONN` except the two tail targets move to this function's
branches (sink path `0x1baa04`, which expects `x8 = [0x3f6a70]`; src path `0x1baa48`):

```asm
    ... (idx/sep computation identical) ...
    cmp  w11, #1
    b.eq 3f
    cbnz w11, 4f
    ldr  x8, [x21, #0xa70]
    b    #0x1baa04                  ; sink path
3:  b    #0x1baa48                  ; src path
4:  ldr  x8, [x21, #0xa70]
    cbz  x8, #0x1baa48
    b    #0x1baa04
```

### 1.4 The CONNECTING-before-SEP edge (answers questions §A2)

`CONNECTING` (state 1) is reported from the **idle** handler *before* SEP discovery, when `slot+0x12` may
still be 0 — so an outgoing connect to the Car would momentarily mis-route to the Sink service on the
first report. Two-part handling:

1. **Seed the slot role at connect time.** For outbound connects the intent is known from the
   `btif_queue` request UUID (`0x110A` → we are Source → remote is SNK → seed `slot.peer_sep=1`;
   `0x110B` → remote is SRC → seed `0`). `connect_int` (`0x1bd684`) already has the UUID in `w20`; a second
   small cave there can `strb` the seed into `&btif_av_cb[idx]+0x12` before the first
   `btif_report_connection_state`. For inbound connects the role is unknown until SEP discovery, so report
   CONNECTING to **both or neither** and let the OPEN report (where `peer_sep` is set, §0.1) settle it.
2. Because the fix in 1.3 falls back to stock when `sep==0xFF`, the only misroute window is this first
   CONNECTING; the decisive CONNECTED/OPEN report is always correct. Seeding closes the window entirely.

### 1.5 Scope note

The same transform applies to every remaining report that keys on "which table is registered" rather than
the slot role (questions §A3): `btif_report_source_codec_state`, the sink `audio_config_cb`, active-device
reporting, and the RC open/close split (Deliverable 2). Each is the same one-instruction-redirect +
shared-idx/sep-cave pattern; they can share a single cave that returns `sep` in `w11`.

---

## Deliverable 2 — P7/P8 (and P10/P11): iPhone AVRCP metadata & pause

### 2.1 P7 — wrong Company ID in outgoing vendor commands

`p_bta_av_cfg` (`[0x3def20]`) is one global, written only in `bta_av_api_register` @ `0x1288a4`:
`0x110B`→ sink table `0x3d22f0` (company `0x001958` `AVRC_CO_METADATA`), `0x110A`→ source table
`0x3d2260` (company `0xDAA119`). In dual role the source registration runs and leaves the **source** table
in effect, so the controller's `GetCapabilities` goes out with `da a1 19` and the iPhone replies
`REJECTED` (snoop in `q2_avctp_frames.txt` / P7).

`BTA_AvVendorCmd` @ `0x126e54` reads that company id and stamps it into the AVRCP vendor PDU:

```
0x00126eb0: ldr x8, [x11, #0xf20]   ; x8 = p_bta_av_cfg (0x3def20)
0x00126eb8: ldr w8, [x8]            ; w8 = company_id  (0xDAA119 in dual role)
0x00126ec4: str w8, [x0, #0xc]      ; -> msg->company_id
```

AVRCP metadata vendor PDUs must always carry the Bluetooth-SIG id `0x001958`, regardless of CT/TG role
(`BTA_AvVendorCmd` is the **CT**-side sender; TG responses use `BTA_AvVendorRsp`, untouched). The surgical
fix is to force the constant — a single in-place instruction, no cave:

```c
// v6 BTA_AvVendorCmd: company id is always AVRC_CO_METADATA for AVRCP
msg->company_id = AVRC_CO_METADATA;   // 0x001958
```

#### Static ARM64 Instruction Table — P7 `BTA_AvVendorCmd` (0x126e54)

| Virtual addr | Original instruction | Original bytes (LE) | Proposed replacement | Replacement bytes (LE) |
|---|---|---|---|---|
| `0x126eb8` | `ldr w8, [x8]` | `08 01 40 b9` | `mov w8, #0x1958` (`movz w8,#0x1958`) | `08 2b 83 52` |

(The now-dead `ldr x8,[x11,#0xf20]` at `0x126eb0` may be left as-is.) **Alternative** (if you prefer reading
the sink table rather than hardcoding): repoint the load to `0x3d22f0` — `adrp x8,#0x3d2000; add
x8,x8,#0x2f0; ldr w8,[x8]` — which is 3 instructions and needs a 1-word cave; the constant store above is
cheaper and equivalent for AVRCP.

### 2.2 P8 — vendor *responses* routed to the target handler

`btif_rc_handler`'s `BTA_AV_META_MSG_EVT` case @ `0x20cdc0` checks the **TG** callbacks first:

```
0x0020cdc0: adrp x8, #0x400000
0x0020cdc4: ldr  x8, [x8, #0x3e8]   ; bt_rc_callbacks (TG)  [0x4003e8]
0x0020cdc8: cbz  x8, #0x20cfe4      ; TG NULL -> controller branch
            ... TG present -> ... -> 0x20d0ec: b handle_rc_metamsg_cmd (0x209d20)
```

The controller branch at `0x20cfe4` already routes correctly by ctype
(`0x20d164: ldrb w8,[x19,#5]; and #0xf8; cmp #8; b.eq -> handle_avk_rc_metamsg_rsp (0x20d1b8/0x20d1d0)`,
`cmp #4; b.ls -> handle_avk_rc_metamsg_cmd (0x20d1a0/0x20dbec)`), **but it only runs when TG is NULL**. In
dual role `A2dpService` brings the AVRCP TG up, so the iPhone's **responses** (ctype ≥ 8 = `AVRC_RSP_*`)
are fed to `handle_rc_metamsg_cmd`, parsed as commands (`avrc_pars_vendor_cmd: length is incorrect,
status 3`), and time out (`handle_get_capability_response: 0xFE` ~2 s later, P8).

Fix: test the incoming **ctype** first; a response with CT present goes straight to the controller
response handler; everything else keeps the existing TG-first path.

```c
// v6 btif_rc_handler, BTA_AV_META_MSG_EVT
uint8_t code = p_data->meta_msg.code;          // x19+5
if (code >= AVRC_RSP_NOT_IMPL /*8*/ && bt_rc_ctrl_callbacks /*[0x4003e0]*/)
    return handle_avk_rc_metamsg_rsp(&p_data->meta_msg);     // 0x20d1d0
/* else fall through to the stock TG-first dispatch (commands) */
```

`0x20d1b8` already does `mov x0,x19` + the function's full epilogue before tail-calling
`handle_avk_rc_metamsg_rsp`, so the cave can branch straight to it.

#### Static ARM64 Instruction Table — P8 `btif_rc_handler` (0x20cdc0)

| Virtual addr | Original instruction | Proposed replacement |
|---|---|---|
| `0x20cdc0` | `adrp x8, #0x400000` | `b CAVE_P8_META` |

`CAVE_P8_META` (reproduces the two instructions it displaces, then continues at `0x20cdcc`):

```asm
CAVE_P8_META:
    ldrb w9, [x19, #5]              ; code (ctype)
    cmp  w9, #8                     ; AVRC_RSP_NOT_IMPL and above = responses
    b.lo 1f
    adrp x9, #0x400000
    ldr  x9, [x9, #0x3e0]           ; bt_rc_ctrl_callbacks (CT)  [0x4003e0]
    cbz  x9, 1f
    b    #0x20d1b8                  ; response + CT up -> handle_avk_rc_metamsg_rsp
1:  adrp x8, #0x400000              ; reproduce 0x20cdc0..0x20cdc8
    ldr  x8, [x8, #0x3e8]           ; bt_rc_callbacks (TG)
    cbz  x8, #0x20cfe4
    b    #0x20cdcc                  ; continue stock TG path
```

Nothing branches into `0x20cdc4`/`0x20cdc8` except fall-through (the jump table targets only `0x20cdc0`),
so leaving those two words in place after the redirect is harmless.

### 2.3 P10 (volume) and P11 (pause) follow from P7+P8

Both are downstream of the event-registration failure, not separate bugs:

- **P10** — with the response path fixed (P8), the iPhone's `GetCapabilities(EVENTS)` is parsed by the
  controller and the CT learns the iPhone's `VOLUME_CHANGED (0x0d)` support; the TG side's missing `0x0d`
  in its own capability reply is a separate TG-capabilities item, but absolute-volume *registration* starts
  working once responses are no longer swallowed.
- **P11** — once responses register, the session reports a real play status (`actions != 0`), so the
  framework stops dropping `pause()`. The native fix is P7+P8; the **launcher** mitigation for the window
  before that lands is Deliverable 4.

---

## Deliverable 3 — P4: `bta_av_co` single active peer vs the sink decoder

### 3.1 What `SetActivePeer(car)` does during iPhone→tablet sink playback

`bta_av_co_set_active_peer` @ `0x2262dc` walks the 5 `BtaAvCo` peer entries (base `0x406e30`, stride
`0x478`), matches by `bd_addr`, sets that peer's active flag, clears the others
(`strb wzr,[x20,#0x40]`), and — if the newly active peer's codec differs — calls
`bta_av_co_audio_set_codec` + `BTA_AvReconfig` (`0x226418`) with a "trigger reconfig again" log.
`bta_av_co_get_active_peer` @ `0x226124` reads the same single-active model (checks `0x406e70`,
`0x4072e8`, `0x407760`, `0x407bd8`, `0x408050` in order → index 0..4, returns `&peer[idx]` at
base `0x406a10`, stride `0x478`; else logs `peer index out of bounds: 5` and returns NULL).

So `SetActivePeer(car)` makes the Car the **one** active peer and reconfigures the **source encoder** path
toward it. This is entirely a **source/encoder** action.

### 3.2 Does the sink SW decoder depend on `active_peer`? — No. (PROVEN from the listings)

The iPhone→tablet decode path never calls `bta_av_co_get_active_peer`:

- `bte_av_sink_media_callback` @ `0x1bef8c` keys on `btif_av_get_current_playing_dev_idx` (`0x1bc598`)
  and enqueues via `btif_a2dp_sink_enqueue_buf` (`0x1a52a4`).
- `btif_a2dp_sink_enqueue_buf` uses the fixed queue `[0x3f3be0]` and the `rx_flush` byte `[0x3f3be8]`,
  then starts `BtifAvrcpAudioTrack*` decoding — no `bta_av_co` call anywhere.
- `btif_a2dp_sink_handle_inc_media` @ `0x1a5814` decodes SBC frames (`OI_CODEC_SBC_DecodeFrame`),
  gated only by `btif_av_get_peer_sep` (`0x1c1cc8`) and `rx_flush`.

`bta_av_co_get_active_peer` is read only by the **source encoder / QTI split-offload** side
(`bta_av_co_get_peer_codec_info 0x227ffc`, `bta_av_co_get_peer_params 0x229afc`,
`bta_av_co_get_encoder_interface 0x22b644`). The on-device proof is already in the logs: pure sink
playback runs **while** `bta_av_co_get_active_peer: peer index out of bounds: 5` and
`No active peer codec config found, by default splitmode` are printed (questions §"What works";
`02_TIMELINE` 12:00) — i.e. the decoder works with **no** `BtaAvCo` active peer. (2026-11-03 `dis_sink_media.txt`
+ 2026-11-04 `bta_av_co_*` listings.)

### 3.3 The real cross-contamination is the **global `peer_sep`**, not `active_peer`

`SetActivePeer(car)` will not stop the decoder, but **opening the Car does**, through a different global:
`btif_a2dp_sink_handle_inc_media` @ `0x1a5864` does `bl btif_av_get_peer_sep; cmp #1; b.eq 0x1a594c`
("State Changed happened in this tick" → skip decode). `btif_av_get_peer_sep` is the single global byte
`[0x3f6de1]`, written on every OPEN (idle handler `0x1b3b44 strb w10,[x8,#0xde1]`). When the Car opens,
that byte flips to 1 (SNK) and the iPhone's SBC decode is skipped. So:

- `active_peer` → affects only the **source/offload** encoder session (`vendor.qti.hardware.bluetooth_audio`,
  `sbc-aac` offload). `SetActivePeer(car)` + its `BTA_AvReconfig` can disrupt/retune an **in-progress source
  stream**, but leaves the sink fixed-queue/track alone.
- **global `peer_sep`** → the actual hazard to the sink decoder, and it is the same global P2 flags.

**Fix direction** (matches questions §A4): make the sink decode path key off the iPhone's **slot**
`peer_sep` (index from `btif_av_get_current_playing_dev_idx`, then `btif_av_cb[idx]+0x12`) instead of the
global `0x1c1cc8` byte; and, for the encoder, split `active_peer_` into `active_source_peer_` /
`active_sink_peer_` so `SetActivePeer` for the car cannot clear the phone's decode context. The split-peer
work is larger than a one-word redirect and would need a dedicated cave (feasible, but size it like v5's
`0xf5118` cave); the global→slot `peer_sep` change at `0x1a5864` is the small, high-value half and is the
same cave/helper pattern as Deliverable 1.

---

## Deliverable 4 — P11/P18/P19: `carhome` launcher player card

Applied to `source/carhome/app/src/main/java/com/local/carhome/CarState.kt` and `Home.kt`. These are
launcher-side mitigations for the window before the native P7/P8 fix lands; they do not depend on it.

### 4.1 `CarState.kt`

Three defects, all stemming from trusting the Bluetooth session's `PlaybackState` (which sits at
`state=7 "Bluetooth audio disconnected", actions=0, metadata=null` until AVRCP event registration):

1. **`pickSession` dropped the BT session** unless it had metadata *and* a PLAYING/PAUSED state — i.e. it
   discarded it exactly in the broken state. Now the BT session is surfaced as soon as it exists and is
   preferred over a stale local session while driving; `mediaActive` marks when the phone's BT session is
   the current controller.
2. **`publishMedia` reported `playing` straight from `state`.** Now `reliable()` distinguishes a trustworthy
   state (`actions != 0` and PLAYING/PAUSED) from the stale BT state; when stale it uses an optimistic flag.
3. **`playPause` always sent PLAY** (because `media.playing` was always false). Now: reliable state →
   explicit `play()`/`pause()`; stale state → an explicit `KEYCODE_MEDIA_PLAY`/`KEYCODE_MEDIA_PAUSE` via
   `AudioManager.dispatchMediaKeyEvent` (bypasses the `actions==0` gate that swallows `transportControls`),
   toggling the optimistic flag so the button and icon stop sticking on Play.

```diff
 import android.os.BatteryManager
 import android.os.Bundle
 import android.os.Handler
 import android.os.Looper
+import android.os.SystemClock
+import android.view.KeyEvent

     var media by mutableStateOf<NowPlaying?>(null); private set
+    /** True while the phone's Bluetooth session is the current controller, even if it reports a stale/idle state. */
+    var mediaActive by mutableStateOf(false); private set
     val heading = Heading(ctx)

-    private fun pickSession() {
-        val list = runCatching { msm.getActiveSessions(listener) }.getOrDefault(emptyList()).toMutableList()
-        btSession?.let { bt ->
-            val st = bt.playbackState?.state
-            if (list.none { it.sessionToken == bt.sessionToken } && bt.metadata != null &&
-                (st == PlaybackState.STATE_PLAYING || st == PlaybackState.STATE_PAUSED)) list.add(bt)
-        }
-        val best = list.firstOrNull { it.playbackState?.state == PlaybackState.STATE_PLAYING } ?: list.firstOrNull()
-        if (best?.sessionToken != controller?.sessionToken) {
-            controller?.unregisterCallback(mcb)
-            controller = best
-            best?.registerCallback(mcb, main)
-        }
-        publishMedia()
-    }
+    private fun pickSession() {
+        val list = runCatching { msm.getActiveSessions(listener) }.getOrDefault(emptyList()).toMutableList()
+        val bt = btSession
+        // Surface the Bluetooth session as soon as it exists (it sits at state=7/actions=0/metadata=null
+        // during real playback until AVRCP event registration; the old metadata/PLAYING gate dropped it then).
+        if (bt != null && list.none { it.sessionToken == bt.sessionToken }) list.add(bt)
+        val best = list.firstOrNull { it.playbackState?.state == PlaybackState.STATE_PLAYING }
+            ?: bt?.takeIf { b -> list.any { it.sessionToken == b.sessionToken } }
+            ?: list.firstOrNull()
+        if (best?.sessionToken != controller?.sessionToken) {
+            controller?.unregisterCallback(mcb); controller = best; best?.registerCallback(mcb, main)
+            optimisticPlaying = null
+        }
+        mediaActive = best != null && best.sessionToken == bt?.sessionToken
+        publishMedia()
+    }

     private var trackApp = ""
+    /** What the last button press intended, kept while the session's own state is unreliable (actions=0). */
+    private var optimisticPlaying: Boolean? = null

-        val c = controller ?: run { media = null; return }
+        val c = controller ?: run { media = null; mediaActive = false; return }
 ...
-        val ps = c.playbackState
-        media = NowPlaying(title, artist, trackApp, c.packageName, trackArt, ps?.state == PlaybackState.STATE_PLAYING,
-            m?.getLong(MediaMetadata.METADATA_KEY_DURATION) ?: 0L, ps?.position ?: 0L, ps?.lastPositionUpdateTime ?: 0L, ps?.playbackSpeed ?: 1f)
+        val ps = c.playbackState
+        if (reliable(ps)) optimisticPlaying = null
+        val playing = if (reliable(ps)) ps!!.state == PlaybackState.STATE_PLAYING else (optimisticPlaying ?: false)
+        media = NowPlaying(title, artist, trackApp, c.packageName, trackArt, playing,
+            m?.getLong(MediaMetadata.METADATA_KEY_DURATION) ?: 0L, ps?.position ?: 0L, ps?.lastPositionUpdateTime ?: 0L, ps?.playbackSpeed ?: 1f)

-    fun playPause() { val c = controller ?: return; if (media?.playing == true) c.transportControls.pause() else c.transportControls.play() }
+    private fun reliable(ps: PlaybackState?): Boolean =
+        ps != null && ps.actions != 0L && (ps.state == PlaybackState.STATE_PLAYING || ps.state == PlaybackState.STATE_PAUSED)
+
+    fun playPause() {
+        val c = controller ?: return
+        val ps = c.playbackState
+        if (reliable(ps)) {
+            if (ps!!.state == PlaybackState.STATE_PLAYING) c.transportControls.pause() else c.transportControls.play()
+            optimisticPlaying = null
+        } else {
+            val playing = optimisticPlaying ?: false
+            sendMediaKey(if (playing) KeyEvent.KEYCODE_MEDIA_PAUSE else KeyEvent.KEYCODE_MEDIA_PLAY)
+            optimisticPlaying = !playing
+            publishMedia()
+        }
+    }
     fun next() { controller?.transportControls?.skipToNext() }
     fun prev() { controller?.transportControls?.skipToPrevious() }
+
+    private fun sendMediaKey(code: Int) {
+        val t = SystemClock.uptimeMillis()
+        runCatching {
+            audio.dispatchMediaKeyEvent(KeyEvent(t, t, KeyEvent.ACTION_DOWN, code, 0))
+            audio.dispatchMediaKeyEvent(KeyEvent(t, t, KeyEvent.ACTION_UP, code, 0))
+        }
+    }
```

`next()`/`prev()` are left on `transportControls` because the snoop shows AVRCP passthrough NEXT already
reaches the iPhone (P19); routing them through key events too risks double-sending.

### 4.2 `Home.kt`

`showMusic` only turned on when `media.playing == true`, so during real playback (stale session) the Sound
card stayed up over the music. Now it also shows whenever a **connected** phone's BT session is current
(`car.mediaActive && phoneBtOn`), keeping the 60 s linger after it drops. The Crossfade still guards on
`media != null`.

```diff
     var showMusic by remember { mutableStateOf(false) }
     val media = car.media
-    LaunchedEffect(media?.playing, media == null) {
-        when {
-            media?.playing == true -> showMusic = true
-            media == null -> showMusic = false
-            else -> { delay(60_000); showMusic = false }
-        }
-    }
+    val phoneBtOn = bt.phones.any { it.connected }
+    LaunchedEffect(media?.playing, media == null, car.mediaActive && phoneBtOn) {
+        when {
+            media?.playing == true || (car.mediaActive && phoneBtOn) -> showMusic = true
+            media == null -> showMusic = false
+            else -> { delay(60_000); showMusic = false }
+        }
+    }
```

### 4.3 P20 (cover art) — not fixable launcher-side

`Home.kt:Cover` and `CarState.publishMedia` already read `METADATA_KEY_ALBUM_ART`/`METADATA_KEY_ART` when
present. No art ever arrives because AVRCP cover-art (BIP) is off in the stack and, even with it on, the
image handle rides track metadata that P7/P8 block in dual role. This stays a stack item, not a launcher
one (`01_PROBLEMS.md` P20).

---

## Summary of proposed native patch sites (spec only — not applied)

| ID | Function @ addr | Site | Change | Cave? |
|---|---|---|---|---|
| P1 | `btif_report_connection_state` `0x1ba370` | `0x1ba394` | redirect → per-slot `peer_sep` dispatch | yes |
| P2 | `btif_report_audio_state` `0x1ba9dc` | `0x1baa00` | redirect → per-slot `peer_sep` dispatch | yes |
| P2' | (edge) `connect_int` `0x1bd684` | seed `slot+0x12` from queue UUID | optional, closes CONNECTING window | yes |
| P7 | `BTA_AvVendorCmd` `0x126e54` | `0x126eb8` | `mov w8,#0x1958` (force `AVRC_CO_METADATA`) | no |
| P8 | `btif_rc_handler` `0x20cdc0` | `0x20cdc0` | ctype-first response routing to CT | yes |
| P4 | `btif_a2dp_sink_handle_inc_media` `0x1a5864` | sink decode gate | global→slot `peer_sep`; (larger) split `active_peer_` | yes |

Launcher (P11/P18/P19) is applied in `CarState.kt` + `Home.kt` above.
