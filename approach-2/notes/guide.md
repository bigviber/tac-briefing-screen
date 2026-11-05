# NEW FILE TO BE ADDED INTO SYSTEM — Lenovo Tab P11 (TB-J606F)

> Device context file for the ADB devices folder. Covers identity, goal, stock ADB setup, optional LineageOS GSI flash, caveats, abort criteria and rollback.
> Status: **purchased 2026-11-02 (PayMore Brampton, ~C$155). Not yet set up. GPS not yet tested.**

---

## 1. Device identity

| Field | Value |
|---|---|
| Model | Lenovo Tab P11 (1st Gen) — **TB-J606F** (Wi-Fi only). NOT P11 Plus (TB-J616F, MediaTek) |
| Store listing | Mislabelled "Tab P11 Plus 64GB Black J606F", SKU ON11-TP223-D1R1 |
| Build on arrival | `TB-J606F_S120184_210520_ROW` (May 2021, Android 10 line) |
| Kernel on arrival | `4.19.95-perf+` |
| SoC | Qualcomm Snapdragon 662 (SM6115P, "bengal"), Adreno 610 |
| RAM / storage | 4 GB / 64 GB UFS, microSD slot |
| Display | 11" IPS 2000×1200, 60 Hz |
| Battery | 7500 mAh, pack **L20D2P32** (FRU SB18E00342), 3.86 V nominal |
| Charging | USB-C 2.0, PD + QC3, 20 W (10 V / 2 A); 4-pin pogo connector on bottom edge |
| Audio | 4 speakers (Dolby Atmos), **no headphone jack**, BT 5.1 (SBC / aptX, no AAC) |
| Location | GPS + GLONASS on Wi-Fi model. **No A-GPS** (cold fix is slow) |
| Size / weight | 258.4 × 163 × 7.5 mm, 490 g |
| Partitions | A/B slots, dynamic `super` (system/system_ext/vendor/product), separate `recovery_a/b`, `vbmeta_a/b` + `vbmeta_system_a/b` |
| Rated temps | Operating 0–40 °C, storage −20–60 °C |

## 2. Goal

Permanently mounted in-car offline navigation + music screen, replacing broken Chinese portable CarPlay screens.

- Landscape-locked, no lockscreen, boots into a car launcher.
- Offline maps (Organic Maps / OsmAnd~ from F-Droid, or Magic Earth). Maps updated at home over Wi-Fi.
- Configured over **ADB, no root**.
- Mounted on existing 4-tab ("4-claw") CarPlay bracket.
- Powered from ignition-switched USB-C. **Battery stays in.**

## 3. Plan — two attempts

0. **Verify GPS** (blocking — return to store if it fails). See §4.

**ATTEMPT 1 — Stock firmware + ADB (easier, try first). See §5.**
Factory reset → take all Lenovo OTA updates → debloat over ADB → install apps → ADB car-screen setup → daily-drive ~1 week and evaluate. If it does everything needed, **stop here — no ROM flash.**

**ATTEMPT 2 — LineageOS 22.2 GSI (only if Attempt 1 falls short). See §6.**
Unlock → flash LineageOS **22.2** GSI (Android 15). Do **not** chase 23.2. If the GSI misbehaves → roll back to stock (§8) and stay on Attempt 1.

---

## 4. Phase 0 — GPS check (do first, within return window)

1. Install **GPS Test** (or open Google Maps).
2. Go outside, clear sky. Allow 2–5 min for first fix (no A-GPS).
3. Pass = position fix, 8+ satellites in use, accuracy ≲ 10–15 m.
4. Fail after 5–10 min outdoors → **return it**.

Known: Notebookcheck measured ~35 m accuracy outdoors, no indoor fix, route-straying in cities on stock. Expect mediocre, not broken.

---

## 5. ATTEMPT 1 — Stock firmware + ADB (try first)

### 5.0 Factory reset + update
1. Settings → System → Reset → **Erase all data (factory reset)**.
2. Go through setup. Connect Wi-Fi. Google account optional (skip it if you plan device-owner kiosk in 5.3; you can sideload apps instead).
3. Settings → System → **System update** → install every OTA until it says up to date (latest known ROW: `S320383_240603_ROW`, Android 11). Arrived on `S120184_210520_ROW`, so expect several updates.
4. After updating, record the final build number here: `________________`

### 5.1 Prep on device
- Settings → About → tap **Build number** ×7 → Developer options → **USB debugging ON**.
- Settings → Battery → **Battery protection mode ON** (caps charge at **60%**). Re-check after every OTA; Lenovo re-enables/disables it after updates.
- Remove Google account if you want device-owner kiosk (see 5.3).

### 5.2 Baseline commands (device-agnostic, untested on this unit)
```bash
adb devices

# Record baseline info (also needed for Phase 2 decisions)
adb shell getprop ro.product.model
adb shell getprop ro.build.display.id
adb shell getprop ro.build.version.release
adb shell getprop ro.product.cpu.abilist      # expect arm64-v8a
adb shell getprop ro.treble.enabled           # expect true
adb shell getprop ro.vndk.version
adb shell getprop ro.boot.slot_suffix
adb shell getprop ro.boot.dynamic_partitions  # expect true

# Screen on while charging (1 AC + 2 USB + 4 wireless = 7)
adb shell settings put global stay_on_while_plugged_in 7

# No lockscreen
adb shell locksettings set-disabled true

# Lock landscape (1 = 90°, 3 = 270° — pick based on USB-C port side on the mount)
adb shell settings put system accelerometer_rotation 0
adb shell settings put system user_rotation 1

# Kill animations
adb shell settings put global window_animation_scale 0
adb shell settings put global transition_animation_scale 0
adb shell settings put global animator_duration_scale 0

# Car launcher as home (fill in pkg/activity)
adb shell cmd package set-home-activity <pkg>/<activity>
adb shell appops set <pkg> SYSTEM_ALERT_WINDOW allow

```

### 5.2a Debloat (reversible, no root)
```bash
# Inventory first — save the output to this folder
adb shell pm list packages -s > j606f_system_packages.txt
adb shell pm list packages -3 > j606f_user_packages.txt

# Candidates to review
adb shell pm list packages | grep -iE "lenovo|zui|motorola|facebook|netflix|amazon|tiktok|game|kids|news|music|youtube"

# Disable (reversible)
adb shell pm disable-user --user 0 <package>
# Or remove for user 0 (reversible via install-existing)
adb shell pm uninstall -k --user 0 <package>

# Undo
adb shell pm enable <package>
adb shell cmd package install-existing <package>
```
- **Do not touch:** anything with `systemui`, `launcher` (until your car launcher is set as home), `settings`, `phone`, `telephony`, `bluetooth`, `location`, `gms`/`gsf` (if keeping Google), `packageinstaller`, `permissioncontroller`, `inputmethod`/keyboard, `providers.*`, `qualcomm`/`qti`.
- Disable one batch at a time, reboot, confirm GPS + Wi-Fi + BT still work.
- Log every package disabled here so it can be reversed:

| Package | Action | Date | Notes |
|---|---|---|---|
| | | | |

### 5.3 Optional: true kiosk (lock-task)
- Requires **no accounts on device** (remove Google account first).
- Car launchers are not device-owner apps; pair with an open-source DPC (e.g. open-kiosk):
```bash
adb shell dpm set-device-owner com.openkiosk/.receiver.KioskDeviceAdminReceiver
```
- Allowlist launcher + nav + music apps in the DPC. Enable home/notifications in lock-task features if needed.
- Undo: remove device owner from the DPC app, or factory reset.

### 5.4 Hiding system bars
```bash
adb shell settings put global policy_control immersive.full=*
```
- **May not work** on Android 11+. If not, use the car launcher's / nav app's own immersive mode.

### 5.5 Apps
- Launcher: **AGAMA Car Launcher** (`altergames.carlauncher`, €2.99 after 30-day trial, tablet-friendly). Alternatives: CarWebGuru, AutoZen, Car Launcher.
- Nav: **Organic Maps** (fully offline, unlimited maps), **OsmAnd~** from F-Droid (Play build limits free map downloads to 7), **Magic Earth** (APK, offline, tablets supported).

### 5.6 Attempt 1 evaluation (after ~1 week of driving)
Stay on Attempt 1 if all are true:
- [ ] GPS locks reliably and nav is usable
- [ ] Boots/wakes into the car launcher, landscape, no lockscreen
- [ ] Performance acceptable after debloat (map pan/zoom, app switching)
- [ ] Bluetooth audio to car stereo works
- [ ] Battery protection (60%) stays on, no heat/swelling concerns
- [ ] No Lenovo bloat/popups that can't be disabled over ADB

Move to Attempt 2 only if a box fails **and** LineageOS would plausibly fix it (bloat, lag, UI restrictions). Note: Attempt 2 loses the 60% charge cap and Widevine L1.

---

## 6. ATTEMPT 2 — LineageOS 22.2 GSI (only if Attempt 1 falls short)

### 6.1 Facts
- **No official LineageOS** for J606F/J606L. Path = **Treble GSI** over Lenovo vendor.
- Target: **LineageOS 22.2 GSI (Android 15)** per XDA guide "[GUIDE][UNOFFICIAL][15] LineageOS 22.2 GSI (TB-J606F)" — https://xdaforums.com/t/guide-unofficial-15-lineageos-22-2-gsi-tb-j606f.4767442/ (**body not yet read — read it before flashing and follow it over this file where they conflict**).
- GSI type: **arm64, A/B**. Use **EXT4**, not EROFS (J606F kernel is 4.19; EROFS needs 5.4+).
- Fallback/advanced (not recommended first): MisterZtr LineageOS 23.2 GSI EXT4 (`v2026.05.24-lineage23.2`, Android 16, claims Android 11 vendor support). Unresolved XDA report: stuck at splash on Lineage 23 (thread 4793141).

### 6.2 What you lose
- **Data wiped** on unlock and again on flash.
- **Widevine L1 → L3** (Netflix etc. SD only). SafetyNet/Play Integrity fails.
- **Stock 60% battery protection is gone.** LineageOS charge control needs a device-specific HAL that GSIs lack.
- Manual updates only (no OTA).

### 6.3 Pre-flight (do not skip)
1. Download **full** stock firmware `TB-J606F_S320383_240603_ROW` (NOT the `_SVC` package — on the sibling P11 Pro it lacked `vendor` and bootlooped):
   https://mirrors-obs-1.lolinet.com/firmware/lenowow/2020/Tab_P11/TB-J606F/
2. Be on the latest stock before unlocking (GSI notes target an Android 11 vendor).
3. Battery **> 50%** (bootloader refuses flashing at low battery).
4. Latest Android platform-tools. Windows: Lenovo USB driver + Qualcomm QDLoader 9008 driver (for EDL rescue).
5. Optional but recommended: EDL backup of `persist`, `modemst1`, `modemst2`, `fsg`, `fsc`.
6. Record the getprop baseline from 5.2.
7. Read XDA thread 4767442 fully.

### 6.4 Key combos
- Fastboot / bootloader: **Vol Down + Power** (or `adb reboot bootloader`)
- EDL (9008): power off, **hold Vol Up while plugging USB** (reported, not verified)
- Recovery combo: not found; use `adb reboot recovery` or bootloader menu.

### 6.5 Unlock
```bash
# Developer options: OEM unlocking ON (may need to be online once)
adb reboot bootloader
fastboot devices
fastboot flashing unlock
# On device: Volume keys → "Unlock the Bootloader" → Power. WIPES DATA.
```
- No Lenovo unlock code / sn.img needed for ROW J606F.
- After unlock: orange-state warning at boot (normal).

### 6.6 Flash (fastbootd route — preferred)
```bash
# vbmeta from the CURRENTLY INSTALLED stock firmware package
fastboot --disable-verity --disable-verification flash vbmeta vbmeta.img

fastboot reboot fastboot
fastboot getvar is-userspace                   # must say yes
# Only if flash fails with "Not enough space to resize partition":
fastboot delete-logical-partition product_a
fastboot flash system <lineage-22.2-arm64-ab-ext4>.img

fastboot reboot bootloader
fastboot --disable-verity --disable-verification flash vbmeta vbmeta.img
fastboot erase userdata
fastboot erase metadata
fastboot reboot
```
- Unknown: whether `vbmeta_system` also needs disabling. If boot fails with "device is corrupt", check XDA 4792709.
- Partition step sequence is from the sibling P11 Pro + generic GSI guides; **untested on J606F**.

### 6.7 TWRP route (avoid unless needed)
- `fastboot flash recovery twrp-J606F_3.7.1_12.0-20240414.img` (unofficial, XDA 4666965).
- TWRP **cannot decrypt /data**, **MTP broken**. Format Data (type `yes`) → `adb push system.img /sdcard/` → Install Image → System Image.

### 6.8 Rules
- **Never dirty-flash** a GSI over another GSI → "Can't load Android system". Always wipe.
- `fastboot boot <foreign image>` flipped slot state on J606L — avoid.

---

## 7. Abort criteria → roll back to stock

Roll back and stay stock if any of these persist after one clean reflash:
- Wi-Fi broken or unstable (known post-GSI report, XDA 4537903)
- GPS not locking
- Bootloop / stuck at splash
- Touch, audio, Bluetooth or rotation broken
- Unusably low brightness floor (XDA 4620289) without a fix

Cosmetic losses that are acceptable: double-tap-to-wake, colour-temperature controls, cut-off quick panel, Netflix HD.

## 8. Rollback

**Option A — Lenovo tool (easiest):** Lenovo Software Fix / Rescue and Smart Assistant (LMSA) → Rescue. Re-flashes stock and **re-locks the bootloader**.

**Option B — fastboot:** flash `boot`, `dtbo`, `vbmeta`, `recovery`, `super` from the full S320383 package, then `fastboot -w`.
- Open issue: whether the package's `super` chunks are concatenated (groxaxo `build_super.py`) or flashed as separate images (P11 Pro report). Check package before relying on this.

**Option C — EDL / QFIL (hard brick):** power off → hold Vol Up → plug USB → QDLoader 9008 appears → QFIL with `rawprogram_unsparse0.xml` + `patch0.xml` from the stock package. Use the firehose programmer from inside the Lenovo package (no standalone J606F firehose found).

---

## 9. Power & battery

- **Keep the battery.** No documented J606F battery-less mod. Swelling is the main risk of always-plugged use; mitigate with the 60% cap (stock only), shade/remove in summer.
- Feed from **ignition-switched** USB-C, ≥20 W PD.
- Auto power-on with ignition: **unsolved**. `fastboot oem off-mode-charge 0` untested on J606F (failed on Lenovo Tab M9). Hardware fallback = MCU pulsing the power button.
- Clean shutdown on power loss needs root (`su -c "reboot -p"`). Not available on stock without root.
- Battery-less notes (future, not planned): keep pack's protection/ID board, feed 4.0–4.2 V (not 3.7), 2200–4700 µF at connector, 12 V input to buck. Qualcomm treats battery ID 6.0–8.5 kΩ as "debug board" and may suspend USB input — keep the original ID resistor.

## 10. Mounting

- Existing mount: 4-tab CarPlay bracket (only measured spec found: 23 × 36 mm hook pattern, Printables 1242428).
- Tablet weighs **490 g** (≈2× a CarPlay screen). Do **not** rely on adhesive on the tablet back (aluminium + rubberised strip).
- Preferred: **hard case + rigid plate bolted through the case** (M3 / heat-set inserts) carrying a printed female 4-tab receiver. VHB only as secondary bond.
- Quick test option: 4-tab → 17 mm ball puck → Arkon SP-SB-AMPS → RAM X-Grip UN9U (fits the 163 mm side). Expect droop on a single 17 mm ball; test only.
- Print in **ASA** (HDT ~93 °C), not PETG (~72 °C) or PLA. Dash temps reach 69–93 °C parked. 5 walls, ≥40% infill near hooks.

## 11. Unverified / open items

- XDA thread bodies (4767442, 4537903, 4620289, 4792709, 4791806, 4793141) not read.
- Whether OEM unlocking needs the device online first; whether `flashing unlock_critical` is needed.
- `ro.vndk.version`, need for `vbmeta_system` disable, super partition size (12 vs 8 GiB).
- GPS behaviour on any GSI.
- All kiosk commands and launchers untested on this unit.
- Recovery key combo, EDL test point.

## 12. Sources

- Lenovo PSREF Tab P11: https://psref.lenovo.com/syspool/Sys/PDF/Lenovo_Tablets/Tab_P11/Tab_P11_Spec.html
- Lenovo P11 user guide (60% battery protection): https://knihovna.ff.cuni.cz/wp-content/uploads/sites/91/2024/04/lenovo_tab_p11_ug_cs_202105.pdf
- Stock firmware mirror: https://mirrors-obs-1.lolinet.com/firmware/lenowow/2020/Tab_P11/TB-J606F/
- DroidWin P11 unlock: https://droidwin.com/unlock-bootloader-on-lenovo-p11-p11-pro-p11-plus-tablets/
- XDA 22.2 GSI guide: https://xdaforums.com/t/guide-unofficial-15-lineageos-22-2-gsi-tb-j606f.4767442/
- J606F debrick guide (vbmeta, TWRP route, rollback): https://raw.githubusercontent.com/groxaxo/tb-j606f-debrick-tools/master/LENOVO_P11_DEBRICK_GUIDE.md
- J606F device tree (partitions, TWRP): https://github.com/balazskokai/android_device_lenovo_j606f
- MisterZtr LineageOS GSI: https://github.com/MisterZtr/LineageOS_gsi/releases
- P11 Pro GSI/rollback write-up: https://badraxas.com/comment-jai-ressuscite-une-lenovo-tab-p11-pro-brickee-avec-fastboot-une-gsi-et-beaucoup-trop-de-temps-libre/
- J606L GSI notes (dirty-flash failure): https://github.com/frnikho/Lenovo_J606L_GSI
- Notebookcheck P11 review (GPS, build): https://www.notebookcheck.net/Lenovo-Tab-P11-tablet-review-Starting-at-250-Euros-The-cheapest-11-inch-tablet-is-also-an-all-rounder.527919.0.html
- Android lock task mode: https://developer.android.com/work/dpc/dedicated-devices/lock-task-mode
- open-kiosk: https://github.com/pedroberaldo87/open-kiosk
- Full research report: `Lenovo Tab P11 J606F LineageOS guide.md`
