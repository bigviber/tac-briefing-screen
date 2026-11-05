# TB-J606F ROM landscape (research 2026-11-02)

Status: first pass from web search and fetch. XDA (xdaforums.com) returns HTTP 403 to the fetch tool and web.archive.org is blocked, so the XDA threads were NOT read. Everything marked "snippet" comes from a search-result summary, not from the page itself. Nothing below has been tested on this tablet.

## Findings
- No maintained device-specific ROM (LineageOS, crDroid, PixelOS etc.) for the J606F was found. The route is Treble GSIs (generic system images) on top of Lenovo's Android 11 vendor.
- XDA guide "[GUIDE][UNOFFICIAL][15] LineageOS 22.2 GSI (TB-J606F)" (thread 4767442): Android 15 GSI. Snippet: uses an EXT4 image because the device kernel does not support EROFS. Per-feature status not read.
- XDA thread "Which ROMs is best for Lenovo P11 TB-J606F" (4659394): snippets only. One user reports an Andy Yan GSI works best on the J606F, another still runs an Android 13 phh-treble build, another says a LineageOS 20 GSI worked except Wi-Fi (poster may mean a different variant; unconfirmed).
- J606L sibling (same Snapdragon 662 / bengal, Lenovo Android 11 vendor): github.com/frnikho/Lenovo_J606L_GSI builds a LineageOS 23.2 (Android 16) GSI on TrebleDroid. README claims home-screen jank 49% (plain LineageOS 22.2 GSI) -> 18-20% (MisterZtr 23.2 build) -> 2-3% with its CPU-boost patch and tuning. Plain GSIs lack Qualcomm CPU boost, so they feel slower than stock on this SoC. Not confirmed on J606F.
- MisterZtr/LineageOS_gsi releases: A/B arm64 GSIs for LineageOS 22.2, 23.0, 23.2, GAPPS or vanilla, EXT4 or EROFS (J606F needs EXT4 per the XDA snippet). No J606F-specific notes.
- groxaxo debrick guide: unofficial TWRP 3.7.1 for J606F (MTP and /data decryption broken), vbmeta flashed with verity and verification disabled, plain AOSP GSI from phhusson as the generic example.
- balazskokai TWRP device tree (android-11 branch): MTP and decryption do not work.
- Mainline Linux: December 2022 LKML patch adds an initial J606F device tree (simplefb, SD card). Not usable as an OS.
- phhusson treble wiki has no usable J606F page (repo archived Jan 2025).

## User-supplied reports (pasted by the user, source thread not named, unverified)
- A GSI user reports 100% -> 30% battery overnight in standby on a newer GSI (stock ROM lasted 3-4 days with ~1 h daily use).
- Same thread, a later post: installed LineageOS 20 (Android 13), "even faster than 21", battery drain "completely gone"; advises against LineageOS 21 (Android 14) for this tablet. No GPS or Bluetooth results given.
- Reading: the drain looks Android-14-specific, not GSI-wide. If Attempt 2 happens, start from an Android 13 GSI and gate it on an overnight standby test (<2% loss) plus GPS and Bluetooth checks. Not yet reflected in guide.md.

- Ponces AOSP Android 13 (github.com/ponces/treble_build_aosp/releases), user comment on the same thread: "works great", "super light and runs fine", "currently the best and most balanced alternative for the P11". Known gap: Double Tap to Wake does not work (said to be a kernel limitation of GSIs on this device). Nothing said about GPS, Bluetooth or standby drain.

- Ponces release check: last Android 13 build is "AOSP 13.0 v2023.09.19" (base android-13.0.0_r82, 4 assets, github.com/ponces/treble_build_aosp/releases?page=4); earlier A13 builds v2023.04.19 to v2023.08.28 (r41..r74). Repo archived 2026-04-08 (read-only, no newer builds). Asset names/sizes not seen yet; open the release page to choose the arm64 A/B file. If Attempt 2 happens, candidate order: Ponces AOSP 13 first, then LineageOS 20; both gated on GPS, Bluetooth and overnight standby <2%.

## What a GSI takes from the vendor (risk list for the car use case)
GPS/location HAL, Bluetooth, audio routing, sensors/auto-rotate, brightness, camera, charging control. The 60% battery cap is lost on GSIs (guide section on LineageOS).

## Open
- Read the XDA threads (user can save pages as HTML into this folder, or paste the key posts).
- Which "close to stock" GSI (vanilla TrebleDroid/AOSP vs LineageOS) boots cleanest here.
