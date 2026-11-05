# What was tried, in order

Short version. `notes/ledger.md` has every step, with exact commands and results.

## 2026-11-02: first attempts

| Time | Step | Result |
|---|---|---|
| Day | Stock analysis: `profile_supported_a2dp_sink=false`, vendor prop `persist.vendor.service.bt.a2dp.sink` | Plan in `notes/dual_bluetooth_plan_2026-11-02.md` |
| 23:25-23:45 | Bootloader unlock, Magisk 30.7 | Root |
| 23:36-23:40 | Overlay plus sink prop | Either/or: the app drops `A2dpService` and adds the sink; `DLOPEN failed for libbthost_if_sink.so` |

## 2026-11-03: making both roles run

| Time | Step | Result |
|---|---|---|
| 00:30 | Receive-only mode (sink prop, `pm enable` of disabled components) | Works |
| 00:45-01:05 | LSPosed hook on `Config.addAudioProfiles` | Both Java services run |
| 02:53-02:56 | Real music | Native abort, `Check failed: queue != NULL`: no sink worker in dual role. 6 Bluetooth restarts, one Android soft restart |
| 03:06 | LSPosed and Zygisk removed | It was flooding `system_server` with malformed binder calls |
| 03:15 | Root cause: CAF `btif_av_init` only does first-role setup | Fix needs binary patches |
| 03:35 | **`btdual_patch` v1:** cave at 0xf5118 starts the sink worker; dex gate bypass | No crash; AVRCP Play reaches the iPhone; stream takes the source path; 3 s suspend |
| 04:04 | **v2:** remote-start flag skip (0x1b6be4), `rx_flush` clear (0x1a4f70) | Continuous stream, nothing decoded |
| 04:36 | **v3:** repeat sink registration | `failed to alloc SCB` x5 |
| 04:41 | Limit 1 | Sink registrations get no btif index |
| 11:40 | Verdict: shared pool, role-blind connect, single active peer | "Multi-function rework" |
| 11:44 | Back to stock receive-only | Verified working |
| 12:00 | **v5:** source registration also creates sink SEPs (codecs 7-9) | **Phone -> tablet decodes and plays in dual mode, confirmed by ear.** Sending to a second device not yet tested |

## 2026-11-04: the uncle's three solutions

| Time | Step | Result |
|---|---|---|
| 00:55 | `carrelay_ivi` v1.0.0 reviewed, built, installed over v5 | One-time Bluetooth restart at first boot, other review items |
| 01:38 | v1.0.1 | Review items fixed |
| 01:45 | Tablet Play | Never reached `onPlay` (see P16) |
| 01:52 | iPhone-initiated Play | Plays; auto-focus overlay works |
| 01:54 | **Dual test with XM6** | **Source link never connects (P1). No mode gets an output.** |
| 02:02 | iPhone would not reconnect | Needed a Bluetooth restart (P6) |
| 02:20 | Disassembly of `btif_report_connection_state` | P1 cause identified |
| 02:23 | v1.0.2: removes the limit override, docs updated | Reboot OK |

## 2026-11-04 evening

| Time | Step | Result |
|---|---|---|
| 19:12 | iPhone connected, Car Home Play | **Works; Next works.** Media-key Pause dropped (P11) |
| 19:15 | Filtered snoop on; Play after a restart; Play with limit 2 | Works both times; limit ruled out (P16) |
| 19:25 | Packets and disassembly | Company ID and dispatch problems (P7, P8), plus P9 and P10 |
| 19:43 | Launcher 1.7 (compass, Bluetooth screen) | Unrelated to the stack. No new permissions |
| 19:49 | Capture run, boot 1 | Came up with limit 2: my earlier revert had only cleared the value in memory. Discarded |
| 19:52 | **Capture run, boot 2, limit 5** | iPhone auto-connects all links; 0xFE and REJECTED again; Play works (4 of 4). Owner skipped the XM6 stage |
| About 19:55 | Reverting the logging, Bluetooth disabled during playback | **Native abort (P12), stream resumed (P13)**; Disconnect stopped it; iPhone did not return (P14); SystemUI crash (P15) |
| After | Owner asked to stop touching the device | Export made. Nothing above has been fixed |

## How v5 differs from stock

`firmware/disassembly/2026-11-04/patch_diff_stock_vs_v5.txt` has the bytes; `notes/btdual_patch_README.txt` explains them.

| Address | Change |
|---|---|
| 0x1c11a8 | `bl btif_av_init` -> cave 0xf5118: also runs `btif_a2dp_sink_startup` |
| 0x1b6be4 | Remote-start flag -> cave: skipped when the sink role is enabled |
| 0x1a4f70 | `btif_a2dp_sink_update_decoder` entry -> cave: clears `rx_flush` byte 0x3f3be8 |
| 0x128da4, 0x128e68 | `bta_av_api_register` source branch loops codecs 0-9; indices 7 and up get tsep 1 and `bta_av_sink_data_cback` |
| Dex 0x179250, 0x179268 | `Config.addAudioProfiles` gates -> `if-eq v1,v1`: both profiles always added |

Nothing has patched `btif_report_connection_state`, `btif_rc_handler` or `p_bta_av_cfg`.
