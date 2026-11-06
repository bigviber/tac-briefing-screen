# Handoff — v6 dual A2DP / AVRCP

Read `notes/v6_defect_review.md` for the full analysis, C reference, and ARM64 instruction tables.
This file is just the task list: what is already done and what you pick up.

## Already done (committed to branch `claude/fervent-goodall-wlvkym`)

- **Launcher player card (P11 / P18 / P19)** — real Kotlin, applied:
  `source/carhome/app/src/main/java/com/local/carhome/CarState.kt` and `Home.kt`.
  Action: build the `carhome` APK and install it. No binary toolchain, no device-stack work.
- **The native defect analysis + patch spec** — `notes/v6_defect_review.md`. Spec only; see below.

## Your job: the native stack patches (spec → bytes → flash → verify)

These were **not** applied. Editing `.so` / `.apk` / `.dex` was out of scope for the static review, so
they exist only as addresses + instruction tables in `notes/v6_defect_review.md`. For each: assemble,
allocate a fresh code cave (same mechanism as v5's `0xf5118`, see `firmware/disassembly/2026-11-04/
v5_patched_sites.txt` and `patch_diff_stock_vs_v5.txt`), patch `libbluetooth_qti.so`, flash via the
Magisk module, verify on device, report logs — exactly the v5 workflow asked for in
`notes/questions_for_uncle_v2.md` §C2.

| Patch | Function @ addr | Site | Cave? | What it fixes |
|---|---|---|---|---|
| **P7** | `BTA_AvVendorCmd` `0x126e54` | `0x126eb8`: `ldr w8,[x8]` → `mov w8,#0x1958` (`08 01 40 b9` → `08 2b 83 52`) | no | Company ID → `AVRC_CO_METADATA`; stops iPhone `GetCapabilities` REJECTED |
| **P8** | `btif_rc_handler` `0x20cdc0` | redirect `0x20cdc0` | yes | Route `AVRC_RSP_*` to the controller handler before the TG check; stops the `0xFE` timeout |
| **P1** | `btif_report_connection_state` `0x1ba370` | redirect `0x1ba394` | yes | Per-peer dispatch via `btif_av_cb[idx]+0x12`; Car link reaches `A2dpService` |
| **P2** | `btif_report_audio_state` `0x1ba9dc` | redirect `0x1baa00` | yes | Same per-peer dispatch for audio state |
| **P2′** | `connect_int` `0x1bd684` | seed `slot+0x12` from queue UUID | yes | Closes the CONNECTING-before-SEP misroute window (optional) |
| **P4** | `btif_a2dp_sink_handle_inc_media` `0x1a5864` | sink-decode gate | yes | Key sink decode off the slot `peer_sep`, not the global `0x3f6de1`; stop the Car OPEN from killing iPhone decode. Separately, split `bta_av_co` `active_peer_` into source/sink. |

## Order / constraints

1. **Start with P7** — one in-place instruction, no cave, independently testable.
2. **P7 + P8 must ship together** (`01_PROBLEMS.md`): company ID alone still times out at `0xFE`;
   routing alone still sends the wrong company ID. Fixing both restores AVRCP event registration, which
   in turn unblocks P10 (volume) and P11 (pause) with no extra native change.
3. **P1 + P2** are the main dual-role blocker; do them as a pair (shared idx/sep cave).
4. **P4** only matters once P1 lands and a source link actually reaches CONNECTED.

## Facts the tables depend on (verify against the binary before patching)

- `btif_av_cb[]` base `0x3f6600`, stride `0xc0`; `peer_bda` at `+0x01`, `sm_handle` at `+0x08`,
  **`peer_sep` at `+0x12`** (`AVDT_TSEP_SRC=0`, `AVDT_TSEP_SNK=1`).
- `0x3dbf30` is the active-index **count** (`btif_max_av_clients`), not the array.
- `bd_addr → index`: `btif_av_idx_by_bdaddr` `0x1af7fc` (returns count when not found).
- Callback tables: `bt_av_src_callbacks [0x3f6a08]` → `A2dpService`; `bt_av_sink_callbacks [0x3f6a70]`
  → `A2dpSinkService`. So `peer_sep==1` → src table, `peer_sep==0` → sink table.
- AVRCP TG callbacks `[0x4003e8]`; CT callbacks `[0x4003e0]`.

## Caveats

- The native tables are **unbuilt and unverified** — produced from the plain-text listings only, with no
  device. Treat them as a patch proposal to assemble and test, not finished bytes.
- `firmware/tools/.../patch_native*.py` and any binary file: not touched here, and not used to produce
  this spec.
