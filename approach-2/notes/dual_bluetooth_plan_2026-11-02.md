# Dual Bluetooth (phone -> tablet -> Buick Verano) - findings and plan (2026-11-02)

Goal: the phone streams audio to the tablet over Bluetooth (tablet = A2DP sink) while the tablet sends audio to the car (tablet = A2DP source).

## What the firmware has (read-only analysis of the files on the tablet, build S320311)
- `com.android.bluetooth` (/system/app/Bluetooth/Bluetooth.apk) contains `A2dpSinkService`, `AvrcpControllerService`, `HeadsetClientService`.
- Its resources (aapt2 dump, `resources.txt`): `profile_supported_a2dp` true, **`profile_supported_a2dp_sink` false**, `profile_supported_avrcp_controller` true, **`profile_supported_hfpclient` false**. These booleans decide which profiles start. Running profiles confirmed in `../bt_profiles_2026-11-02.txt` (no A2dpSink, no HeadsetClient).
- Stack: Qualcomm `libbluetooth_qti.so` (/system_ext/lib64). Strings show per-device sink-role handling (`btif_device_in_sink_role`, `Reset to Sink role`, `SINK role true`) and vendor switches `persist.vendor.service.bt.a2dp.sink`, `persist.vendor.bt.a2dp.sink_conn` (both unset now).
- No overlays target Bluetooth. The shell cannot set vendor properties or install a resource overlay for a platform-signed system app.

## Verdict
Not possible on stock without root. With root it is a configuration change (no code rewrite): flip the two booleans with a resource overlay and set the vendor sink property. Whether source (to car) and sink (from phone) run at the SAME time on this stack is unproven; the per-device sink-role code is a good sign. Needs one real test.

## Route A: root the stock firmware (keeps Lenovo battery protection, unlike a GSI)
1. User present (physical key presses). Settings > Developer options > OEM unlocking ON.
2. `fastboot flashing unlock` -> confirm on the tablet. **This wipes all data.**
3. Flash full stock `firmware/S320383_ROW` (we have it; there is no S320311 image to patch). Needs the matching boot.img for Magisk.
4. Patch S320383 `boot.img` with Magisk on the tablet, flash it to the active slot.
5. Magisk module (we build it): a resource overlay for `com.android.bluetooth` with `profile_supported_a2dp_sink=true` (+ optionally `profile_supported_hfpclient=true`), and `persist.vendor.service.bt.a2dp.sink=true` in system.prop.
6. Test: pair phone -> tablet (sink) and tablet -> car (source); play music on the phone; check `dumpsys bluetooth_manager`.
7. Restore the setup with `reapply.sh` (debloat, Car Home, settings).
Risk: the sink property might switch the stack to sink-only (car audio from the tablet would stop). The module can be removed in Magisk to undo.

## Route B: no root, over Wi-Fi instead of Bluetooth
- iPhone: an AirPlay audio receiver on the tablet (no phone app needed); tablet plays to the car over Bluetooth.
- Android phone: a small sender app on the phone (Android 10+ playback capture) streams to a receiver in Car Home over the tablet's hotspot or the phone's hotspot.
Latency ~100-200 ms (fine for music, not for calls). Calls stay phone -> car directly.

## Route C: hardware
A multipoint Bluetooth receiver into the car's AUX input (phone and tablet both connect to it).

## RESULT after rooting (2026-11-02 23:36-23:40, no phone needed)
1. Module A (resource overlay, profile_supported_a2dp_sink=true) installed and ENABLED (`[x] com.local.btsink.overlay`, /product/overlay),
   but Qualcomm's Bluetooth app ignores that resource: `AdapterServiceConfig: addAudioProfiles isA2dpSink :false ... Profile A2dpSinkService Not added`.
2. The real gate is the vendor property `persist.vendor.service.bt.a2dp.sink` (tested in memory only with `resetprop`, Bluetooth restarted):
   `isA2dpSink :true` -> `Profile A2dpService Not added` + `Adding A2dpSinkService`. It is EITHER/OR in the firmware's Bluetooth app:
   the tablet can send to the car OR receive from a phone, never both.
3. Even sink-only mode does not come up: Bluetooth stayed in TURNING_ON, no sink service ran; the audio HAL lacks the sink library
   (`a2dp_offload: open_a2dp_sink: DLOPEN failed for libbthost_if_sink.so`).
4. Reverted: property cleared, module removed, Bluetooth back to normal (A2dpService running), reboot check clean.

Verdict: dual Bluetooth is NOT achievable on this stock firmware even with root, short of rewriting the vendor Bluetooth app and
supplying the missing audio library (no source code; very high risk, low chance). Practical routes: (B) phone -> tablet over Wi-Fi
(AirPlay receiver if iPhone, or our own small sender for Android), tablet -> car over Bluetooth; (C) a multipoint Bluetooth receiver
on the car's AUX input; (D) tablet in a future custom ROM, still unproven.

## UPDATE 2026-11-03: receive mode WORKS; the either/or gate is two lines of Java
- The earlier stall was not the native stack: `ActivityManager: Unable to start service ... a2dpsink.A2dpSinkService ... not found` -
  the component is disabled in the manifest (android:enabled comes from profile_supported_a2dp_sink at install time; overlays don't
  reach manifest flags). Fix (root, persists): `pm enable com.android.bluetooth/.a2dpsink.A2dpSinkService` (+ `.avrcpcontroller.BluetoothMediaBrowserService`).
- With overlay module A + component enabled + `persist.vendor.service.bt.a2dp.sink=true`: Bluetooth ON in ~6 s,
  `A2dpSinkService: profile started successfully`, native `btif_a2dp_sink_w` thread running. Receive-only mode is functional.
- Gate (dexdump of Config.addAudioProfiles): returns false for "A2dpSinkService" when the prop is false, and false for "A2dpService"
  when the prop is true; everything else true. Pure Java.
- Starting A2dpSinkService from outside (`am startservice ... --es action com.android.bluetooth.btservice.action.STATE_CHANGED
  --ei android.bluetooth.adapter.extra.STATE 12`) is refused unless the adapter is in TURNING_ON (11) ("intent received late"), and an
  extra profile would break AdapterService's started-profile count. Not a reliable route.
- Plan: Zygisk + LSPosed + our own tiny Xposed module that hooks Config.addAudioProfiles in com.android.bluetooth so "A2dpService"
  is always added. With the sink prop on, Android's own startup then brings up source AND sink. Open question after that:
  whether Qualcomm's native AV layer accepts both roles (BTA_AvEnable is called by both).

## RESULT 2026-11-03 01:05: both roles running
LSPosed 1.9.2 (Zygisk) + our module com.local.btdual + sink property (resetprop -p) + A2dpSinkService component enabled:
`Adding A2dpService` + `Adding A2dpSinkService`, both profiles started, survives reboot, 0 crashes. Received audio is played by the
stack through android::AudioTrack, so it follows Android's normal output routing (car, when the car is connected).
Still to prove with real devices: (1) phone pairs and streams to the tablet, (2) at the same time the tablet streams to the car,
(3) the phone's music is heard in the car. Re-create: root/reapply_root.sh.
