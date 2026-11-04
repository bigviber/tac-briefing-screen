# Car IVI Bluetooth Relay — Lenovo TB-J606F (Android 11 / SDK 30)

Reference head-unit platform that takes an **iPhone's A2DP stream in** (tablet =
A2DP Sink + AVRCP Controller + HFP Client) and pushes it **out to the car
stereo** (tablet = A2DP Source), with three switchable routing architectures and
a landscape Jetpack Compose dashboard.

Hardware: Lenovo Tab P11 `TB-J606F`, Snapdragon 662 (SM6115), WCN3980, stock
`S320311_230810_ROW`, kernel 4.19.157, Magisk 30.7. The audit of the live device
dumps is in [`docs/HARDWARE_AUDIT.md`](docs/HARDWARE_AUDIT.md).

## Layout

```
.
├── build_and_package.ps1                 # RRO (aapt2) + app (Gradle/CMake) + Magisk zip
├── carrelay-core/                        # shared contracts, jOOR hidden-API helpers, A2DP-in publisher
├── solution1-native-stack/
│   ├── magisk-module/                    # module.prop, system.prop, post-fs-data.sh, service.sh,
│   │                                     # customize.sh, META-INF, privapp-permissions-carrelay.xml
│   └── rro/CarIvIBtOverlay/              # static RRO on com.android.bluetooth (bools.xml)
├── solution2-oboe-bridge-app/app/        # privileged companion app
│   └── src/main/cpp/                     # OboeAudioBridge.cpp (FullDuplexStream), RingBuffer.h, jni_bridge.cpp
├── solution3-audiopatch-bridge/          # AudioSystem.createAudioPatch + uid AudioPolicy bridges
└── docs/HARDWARE_AUDIT.md
```

Gradle modules: `:carrelay-core`, `:solution3-audiopatch-bridge`,
`:solution2-oboe-bridge-app:app` (Kotlin 2.0.20, AGP 8.6.1, Gradle 8.9, JDK 17,
NDK 26.3, CMake 3.22.1).

## The three architectures

| Mode | Where the PCM moves | Requirements |
|---|---|---|
| **1 · Native focus** (`FocusRoutingEngine`) | Nowhere in our process. `libbluetooth_qti` decodes the sink stream and plays it as a media AudioTrack; APM routes media to the active A2DP device = car. | `CarIvIBtOverlay` (`a2dp_sink_automatically_request_audio_focus=true`) or a Media3 `prepare()` on `BluetoothMediaBrowserService`; `setActiveDevice(car)`. |
| **2 · Oboe duplex** (`OboeBridgeEngine` + `OboeAudioBridge.cpp`) | `oboe::FullDuplexStream`, LowLatency/Shared, input = `TYPE_BLUETOOTH_A2DP` input device, output = car `TYPE_BLUETOOTH_A2DP` device id. Ring buffer with single-frame slip drift compensation, `LatencyTuner`, automatic restart on `ErrorDisconnected`. Fallback/forced: uid-1002 AudioPolicy **loop-back** mix → `AudioRecord` → Oboe push mode. | priv-app with `MODIFY_AUDIO_ROUTING`, `CAPTURE_AUDIO_OUTPUT`, `RECORD_AUDIO`. |
| **3 · AudioPatch** (`AudioPatchBridge`) | `AudioSystem.createAudioPatch(BT A2DP In → BT A2DP Out)`: AudioFlinger PatchPanel software bridge (ports are on different HW modules). Alternative strategy: uid-1002 AudioPolicy **render** mix pinned to the car device. | `MODIFY_AUDIO_ROUTING`; device patch additionally needs the HAL "a2dp input" to carry PCM. |

All three share `BtRoleCoordinator`: strict role pinning via SDK 30
`setConnectionPolicy(device, 100|0)` on `BluetoothA2dp` / `BluetoothA2dpSink` /
`BluetoothHeadsetClient` / `BluetoothHeadset` (jOOR), and the sequencer *car
A2DP source → 2500 ms settle → iPhone A2DP sink (+HFP client) → active device =
car*, triggered by `ACTION_POWER_CONNECTED` or the dashboard.
`BluetoothAvrcpController` has no connection-policy API on SDK 30; the
coordinator reports that and relies on AVRCP CT following the sink link.

## Build & package (Windows PowerShell)

```powershell
# needs ANDROID_HOME with build-tools, platforms/android-30 (or newer), NDK 26.3.11579264, CMake 3.22.1, JDK 17
.\build_and_package.ps1            # -> dist\carrelay_ivi-v1.0.0.zip
.\build_and_package.ps1 -Deploy    # + adb push, magisk --install-module, optional reboot
```

The script generates `keys/carrelay.jks` (self-signed is enough for
`/system/priv-app` + `/product/overlay`), builds `CarIvIBtOverlay.apk` with
aapt2, runs `gradlew :solution2-oboe-bridge-app:app:assembleRelease`, extracts
`lib/arm64-v8a/*.so` next to the APK (system apps are never extracted), and
zips the module with forward-slash entry names.

## After flashing

1. Reboot. `service.sh` enables both overlays, `pm enable`s the sink-side
   services, relaxes `hidden_api_policy`, grants `RECORD_AUDIO`, starts
   `RelayForegroundService`. Logs: `/data/adb/carrelay/{post-fs-data,service}.log`,
   `boot_snapshot.txt`.
2. Open **Car Relay**: pick the iPhone and the car in the two role cards, press
   **Pin roles**, then **Connect sequence + relay**.
3. Switch architectures live with the radio group; the counters card shows
   sample rates, Oboe XRuns, ring fill, slips, latency and restarts.
4. Optional experiment: `touch /data/adb/carrelay/a2dp_in_primary` and reboot to
   move `BT A2DP In` into the primary HAL module (see `post-fs-data.sh`).

Remove the Magisk module to undo everything; the pre-existing
`btsink_overlay_prop`, `btdual_patch`, `carhome_priv` and `hfp_audio` modules
are left untouched.
