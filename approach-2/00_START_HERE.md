# Lenovo Tab P11: dual Bluetooth audit pack

You are being asked for a second set of eyes. **You have no access to the tablet.** The owner and their assistant (Claude Code) have it over USB ADB with root and can run anything you specify, then send you the output. This folder holds everything needed to audit the work without the device: notes, logs, firmware binaries, disassembly and source.

The owner states they are in the EU and are doing this as right-to-repair work on their own device.

## The goal

Permanently mounted car screen (Buick Verano). Audio path, all over Bluetooth at the same time:

    iPhone --A2DP--> tablet (A2DP sink, AVRCP controller, HFP client) --A2DP--> car stereo (tablet = A2DP source)

## Status: NOT solved

| Works | Does not work |
|---|---|
| iPhone -> tablet music (sink decode, audio focus) | **Tablet -> car/headphones A2DP link: never reaches CONNECTED at the Java layer. This is the main blocker.** |
| Both A2DP services running at once | All three routing solutions: blocked, because they never get a car output |
| Tablet Play and Next reach the iPhone (4 of 4 on 2026-11-04) | Track info, play state, phone library (dual role) |
| | Pause from the tablet; iPhone volume following the tablet |
| | Launcher player card: song not shown accurately in real time, buttons struggle to control the phone, no cover art (P18-P20) |

**Read `01_PROBLEMS.md` next.** It lists every open problem with its evidence, and marks what is proven, inferred or unknown. `02_TIMELINE.md` is what was tried and what happened.

## What we want from you

1. **Audit the diagnoses in `01_PROBLEMS.md`.** Do not take them as given. Tell us what is wrong or missing, and check them against the binaries here.
2. **Find the most effective route to true concurrent A2DP source + sink on this hardware.** Options to weigh, including ones not listed:
   - further native patches to this QTI stack (a "v6"): is it realistic, how many sites, what size?
   - a different Bluetooth stack or a GSI: which version really supports source and sink at once?
   - anything that avoids the shared `btif_av` entirely
   - the hardware fallback (Bluetooth transmitter on the 3.5 mm jack), if you conclude the software route is not worth it. Say so plainly.
3. **List everything else that will break after the source link connects.** Candidates: the single active peer in `bta_av_co`, the split-A2DP offload session, audio routing, and interaction with HFP.

## How to answer so we can act on it

We can only test what you specify exactly.

- **Native patches:** function, virtual address, original bytes, replacement bytes, code cave location. This matches how v1-v5 were built (`firmware/tools/fable/patch_native*.py`, `notes/btdual_patch_README.txt`). Addresses are for the exact binaries in `firmware/`.
- **Tests:** exact commands, what to do on the device, what to capture.
- **One change at a time.** We install, reboot-check and return logs after each.

## Device facts

| | |
|---|---|
| Model | Lenovo Tab P11 1st gen, TB-J606F, Wi-Fi only |
| SoC / Bluetooth | Snapdragon 662 (bengal), WCN3980 (`soc_name = cherokee`) |
| Firmware | `Lenovo/LenovoTB-J606F/J606F:11/RKQ1.210303.002/S320311_230810_ROW`, SDK 30, patch 2023-07-05 |
| Kernel | 4.19.157, SELinux enforcing, A/B slot b, dynamic partitions |
| Root | Bootloader unlocked, Magisk 30.7, no Zygisk |
| Stack | Qualcomm `libbluetooth_qti.so` in `/system_ext/lib64`, `Bluetooth.apk` in `/system/app/Bluetooth`; offload capability `sbc-aac` |
| Clock | The tablet has no network, so log timestamps read 2023-08-10/11. They are not real dates. |

Magisk modules on the device: `btdual_patch` v5, `btsink_overlay_prop`, `carrelay_ivi` v1.0.2, `carhome_priv` v1.6.2 (launcher, with update 1.7 installed over it), `hfp_audio` v1. Full list: `device_snapshot/magisk_modules.txt`.

Binary identities, re-checked on the device 2026-11-04 (`device_snapshot/firmware_hashes.txt`):

| File | Stock | Running (v5) |
|---|---|---|
| `libbluetooth_qti.so` | sha1 `bdfef236a142...`, sha256 `16ecec109c0cb624...` | sha1 `da7e2e993afc...` |
| `Bluetooth.apk` | sha1 `66de90cf9b6c...`, sha256 `3599b566cedc327c...` | sha1 `e8f3f9fa89c9...` |

## The requested capture checklist

| Requested | Where | Notes |
|---|---|---|
| `logcat -b all`, boot -> iPhone connect -> Play on tablet -> connect headphones/car | `logs/2026-11-04_full_run/logcat_all_boot_to_play.txt` | **The headphones/car stage is not in this run: the owner skipped it.** The failure is captured in last night's log, `logs/2026-11-04_carrelay/q1_iphone_connect_logcat.txt` (limit 2 at the time, not 5). |
| `log.tag.*` DEBUG set first | Done as `persist.log.tag.*` | Plain `log.tag` does not survive a reboot. These tags already logged at debug level before the props were set. |
| `btsnoop_hci.log`, same run | `logs/2026-11-04_full_run/btsnoop_hci_filtered_sanitized.log` | Android 11 uses `persist.bluetooth.btsnooplogmode`; run in `filtered` mode, then sanitized (see below). |
| `dumpsys media_session`, iPhone connected | `logs/2026-11-04_full_run/dumpsys_media_session_{1,2}_*.txt` | Before and during playback. One Bluetooth session exists. |
| `bt_config.conf`, MACs redacted | `logs/2026-11-04_full_run/bt_config_redacted.conf` | Keys and names removed. Service UUIDs and versions kept. |
| `btdual_patch` as-is + stock originals | `deployed_modules/btdual_patch-v5.zip`, `firmware/stock/` | The patched pair is also in `firmware/patched_v5/`. |

## Folder map

| Path | Contents |
|---|---|
| `01_PROBLEMS.md`, `02_TIMELINE.md` | Start here |
| `notes/` | `ledger.md` (every change, with time and result), `device_CLAUDE.md` (running notes), `FINDINGS.md`, `questions_for_uncle_v2.md` (detailed native questions), `guide.md`, rollback/reapply scripts |
| `logs/2026-11-04_full_run/` | The capture above, plus the crash buffer |
| `logs/2026-11-04_carrelay/` | v1.0.x tests, the XM6 failure, AVCTP frames (`q2_avctp_frames.txt`) |
| `logs/2026-11-02_03_earlier/` | Earlier sink, hook and dual attempts |
| `device_snapshot/` | Read-only state of the tablet: `dumpsys` output, Bluetooth/audio props, config files (`bt_stack.conf`, audio policy), library listing, thread list |
| `firmware/stock/`, `firmware/patched_v5/` | The Bluetooth library, apk and dex; also `classes_dump.txt.zip` (66 MB unzipped), `resources.txt` |
| `firmware/symbols/` | `minidebug.elf` (from the library's `.gnu_debugdata`), `symtab_funcs.txt` |
| `firmware/disassembly/2026-11-04/` | The functions behind the problems in `01_PROBLEMS.md`, the v5 patch diff, the `bta_av_cfg` tables |
| `firmware/disassembly/2026-11-03/` | Earlier analysis |
| `firmware/other_bt_libs/`, `firmware/audio/` | Related libraries pulled from the device; audio HAL, mixer paths, HFP work |
| `firmware/tools/` | Disassembly, snoop and patch scripts. Example: `python dis_rc.py LIB minidebug.elf 20cc10:5c0` (needs capstone and pyelftools) |
| `deployed_modules/` | The Magisk modules as installed |
| `source/` | Our launcher (`carhome`), the removed LSPosed hook (`btdual`), overlay (`btsink`), `hfp_audio` |
| Not in this folder | The three solutions (`tac-briefing-screen-main`, the `carrelay_ivi` module, with `docs/STACK_NOTES.md` and `docs/HARDWARE_AUDIT.md`): the owner provides these separately |
| `SCRUB_REPORT.txt`, `MANIFEST.txt` | What was removed from each file; sha256 of every file |

Addresses are virtual addresses in `libbluetooth_qti.so`; in `.text`, file offset = vaddr. Stock and v5 differ only at the hunks in `firmware/disassembly/2026-11-04/patch_diff_stock_vs_v5.txt`.

## What was removed for privacy

- **Addresses are placeholders:** `AA:00:00:00:00:01` = the iPhone (`USER_IPHONE`), `...:02` = Sony WH-1000XM6 (stand-in for the car), `...:03` = the tablet, `BB:00:...` = any other device. The tablet serial is `TABLET_SERIAL`.
- **Log lines mentioning Wi-Fi, location, accounts, or contacts and calls were deleted** from `logs/` and `device_snapshot/`. Line counts are in `SCRUB_REPORT.txt`. If a gap matters, ask for a targeted re-capture.
- **`btsnoop`:** link keys zeroed, remote name zeroed, inquiry results and LE advertising reports dropped. Payload is kept for L2CAP signalling, SDP, AVCTP and AVDTP (media cut to 32 bytes). RFCOMM, OBEX and everything else is cut to the L2CAP header. So hands-free AT commands and phonebook traffic are not visible.

Not included: full firmware images and partition backups (they hold personal data), screenshots, ADB binaries, the Android toolchain, and the other devices in the household.

## Constraints on anything you propose

- **Magisk modules only.** No flashing, no partition writes, and nothing affecting the bootloader without the owner's explicit approval.
- **Do not suggest LSPosed.** It flooded `system_server` with malformed binder calls and caused soft restarts; removed.
- **Boot-loop risk is real.** A persistent process crashing at boot sent this tablet to recovery on 2026-11-02 (details and recovery recipe in `notes/device_CLAUDE.md`). The privapp whitelist is enforced, so a privileged app requesting an unlisted permission stops boot.
- **Native patches must match these exact binaries** (build S320311).
- **`resetprop name value` on a `persist.` prop persists it**, and `--delete` needs `-p` to remove it. This caught us on 2026-11-04.
- Audio tests run at low volume (3/15).

## State the tablet was left in

Bluetooth on, iPhone **disconnected**, nothing playing, media volume 3/15, temporary logging removed. The owner asked to stop touching the device at that point.
