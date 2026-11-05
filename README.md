# tac-briefing-screen — dual-role Bluetooth (iPhone A2DP sink → car A2DP source) on Lenovo TB-J606F

Target device: Lenovo TB-J606F, Android 11 / SDK 30, stock firmware S320311_230810_ROW, Qualcomm SM6115 with a WCN3980 Bluetooth/Wi-Fi chip, running the QTI Bluetooth stack (`libbluetooth_qti.so`), rooted with Magisk. Goal: receive the iPhone's A2DP stream on the tablet and send it to the car stereo at the same time.

## Approach 1 — overlay + props + privileged companion app (status: does not achieve dual role)

What was built (details in [`approach-1/README.md`](approach-1/README.md)):

- A Magisk module with the `CarIvIBtOverlay` RRO (on `com.android.bluetooth`) and system props.
- A Jetpack Compose companion app (privileged, in `/system/priv-app`) with three routing modes: native focus, Oboe duplex, and AudioPatch.
- An AudioFlinger-side bridge library (`AudioSystem.createAudioPatch` and UID-scoped AudioPolicy mixes).

Verified working on the device ([`approach-1/docs/STACK_NOTES.md`](approach-1/docs/STACK_NOTES.md), [`approach-1/docs/HARDWARE_AUDIT.md`](approach-1/docs/HARDWARE_AUDIT.md)):

- Phone → tablet A2DP sink plays with 0 underruns when the iPhone presses Play.
- Clean boot.
- The overlay, props and privileged app take effect.

What blocks it:

- The tablet cannot start iPhone playback: the `BluetoothMediaBrowserService` session is stuck in `STATE_ERROR` (7) with `actions=0`.
- The A2DP source link to the car opens natively but is reported through the sink JNI, so `A2dpService` never sees CONNECTED. This is a native-stack limitation of `libbluetooth_qti` + `btdual_patch` v5, not a routing problem.

## Approach 2 — device-export driven (pending)

Approach 2 will start from a direct folder export of the device plus Android's native logs (logcat, btsnoop, dumpsys), supplied by the owner. It will live under `approach-2/`; nothing is built yet (see [`approach-2/README.md`](approach-2/README.md)).

Two candidate directions were identified in Approach 1's notes. Neither is built; the choice will be made from the export data.

- (a) Move the car link off the tablet radio: USB-C audio to an external A2DP transmitter.
- (b) Replace the QTI stack with AOSP Fluoride, which keeps separate source and sink peer pools.

## Layout

- `approach-1/` — first attempt (Gradle project, Magisk module, docs, packaging script).
- `approach-2/` — placeholder for the device-export driven attempt.
