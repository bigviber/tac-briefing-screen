# Hardware / Firmware Audit — Lenovo TB-J606F, build S320311_230810_ROW

Source: `j606f_bt_context_bundle.md` (live dumps from the tablet, 2023‑08‑10).

| Item | Value |
|---|---|
| Model / SoC | Lenovo TB-J606F (Tab P11), Qualcomm SM6115 "bengal", WCN3980 (`persist.vendor.qcom.bluetooth.soc=cherokee`) |
| OS | Android 11, **SDK 30**, security patch 2023‑07‑05, kernel 4.19.157‑perf+, SELinux **Enforcing**, Treble, VNDK 30, slot `_b`, verified boot `orange` |
| Bluetooth stack | Qualcomm `libbluetooth_qti.so` (`ro.bluetooth.library_name`), HAL `android.hardware.bluetooth@1.0-service-qti` (pid 843), audio providers `vendor.qti.hardware.bluetooth_audio@2.0/2.1` + `android.hardware.bluetooth.audio@2.0` hosted in the audio HAL (pid 840) |
| A2DP source | **Split-A2DP / DSP offload ON**: `ro.bluetooth.a2dp_offload.supported=true`, `persist.vendor.qcom.bluetooth.enable.splita2dp=true`, `A2dpOffloadEnabled: true`, caps `sbc-aptx-aptxtws-aptxhd-aac-ldac` |
| A2DP sink switch | `persist.vendor.service.bt.a2dp.sink=true` (set by the existing `btsink_overlay_prop` module; unset on stock) |
| MaxConnectedAudioDevices | 5 (already > 1) |
| Magisk | 30.7 with 4 user modules: `btdual_patch` (patched Bluetooth.apk + libbluetooth_qti.so, "source+sink"), `btsink_overlay_prop` (BtSinkOverlay + sink prop), `carhome_priv` (Car Home priv-app), `hfp_audio` (mixer_paths / audio_platform_info fixes + SLIM_7_TX→MultiMedia10 bridge) |
| Hidden API | `hidden_api_policy=null` (default enforcement), exemptions already contain `Landroid/bluetooth/` |

## 1. What `BtSinkOverlay` does today

`/product/overlay/BtSinkOverlay.apk` = package **`com.local.btsink.overlay`** (not a Lenovo overlay; it is the user's own, Magisk-mounted). Static RRO, declared priority 100 (OverlayManager reports `mPriority 10`), `STATE_ENABLED`, target `com.android.bluetooth`, no overlayable. Its resource table contains exactly three entries:

```
bool/profile_supported_a2dp_sink   = true
bool/profile_supported_hfpclient   = true
bool/profile_supported_pbapclient  = true
```

Effective `cmd overlay lookup` results on the live system:

| resource | stock (res_live) | effective |
|---|---|---|
| `profile_supported_a2dp_sink` | false | **true** (overlay) |
| `profile_supported_avrcp_controller` | **true** (already in base APK) | true |
| `profile_supported_hfpclient` | false | **true** (overlay) |
| `profile_supported_pbapclient` | false | **true** (overlay) |
| `a2dp_sink_automatically_request_audio_focus` | **false** | **false** ← not overlaid |
| `profile_supported_a2dp` / `hs_hfp` | true | true |
| `profile_supported_avrcp_target` | false | false |
| `avrcp_controller_enable_cover_art`, `a2dp_sink_audio_focus_request_delay_ms` | **do not exist** on this build | lookup fails |

Consequence: the sink role is on, but `A2dpSinkStreamHandler` never requests audio focus by itself, so the decoded iPhone stream stays silent until a MediaSession `prepare()`/`play()` reaches `AvrcpControllerStateMachine`. That is the gap `CarIvIBtOverlay` closes (`a2dp_sink_automatically_request_audio_focus=true`, static, priority 200).

## 2. `com.android.bluetooth` on this build

* `/system/app/Bluetooth/Bluetooth.apk` (versionCode 30, sharedUserId `android.uid.bluetooth`, uid 1002) is **the btdual_patch copy** (identical sha1 `e8f3f9fa…` for `/system/app/...` and `/data/adb/modules/btdual_patch/...`); same for `/system/system_ext/lib64/libbluetooth_qti.so` (`da7e2e99…`). The `work/manifest_stock.txt` vs `work/manifest_live.txt` diff is empty, so the manifest was not changed by the patch; only dex/native code was.
* Manifest gates: `A2dpSinkService` → `@0x7f02000a profile_supported_a2dp_sink`, `BluetoothMediaBrowserService` → same, `AvrcpControllerService` → `0x7f02000b`, `HeadsetClientService` → `0x7f02000f`, `HfpClientConnectionService` → `0x7f020005 hfp_client_connection_service_enabled` (false), `AvrcpTargetService` → `0x7f02000c` (false), `BATService` (QTI broadcast audio) → `0x7f02000d` (true).
* PackageManager `enabledComponents` already pins: `BluetoothMediaBrowserService`, `PbapClientService`, `AuthenticationService`, `A2dpSinkService`, `HeadsetClientService`.
* **Running right now** (`dumpsys activity services`): AdapterService, GattService, SapService, **AvrcpControllerService**, **HeadsetClientService**, PbapService, **A2dpService (source)**, **BluetoothMediaBrowserService**, PbapClientService, **HeadsetService (AG)**, MapService, HidHostService, OppService, PanService, **A2dpSinkService**. Both A2DP roles and both HFP roles are resident simultaneously.
* `dumpsys bluetooth_manager`: one bonded device (`iphobee`, DUAL), `A2dpSinkService Devices Tracked = 0`, `A2dpService mActiveDevice: null`, `HeadsetService mMaxHeadsetConnections 5`, `sBrowseTree Size: 2`.
* Permissions granted to the Bluetooth app include `BLUETOOTH_PRIVILEGED`, `MODIFY_AUDIO_ROUTING`; private flag `ALLOW_AUDIO_PLAYBACK_CAPTURE` is set (relevant for uid‑scoped capture of its sink AudioTrack).

## 3. Audio policy (what is already wired, what is not)

* APM **Config source: `/vendor/etc/audio/audio_policy_configuration.xml`** (sha1 `b3f5f037…`, 32 417 B). `/vendor/etc/audio_policy_configuration.xml` (16 932 B, QSSI generic) is **not** the one in use. Both include `r_submix_audio_policy_configuration.xml`, `audio_policy_volumes.xml`, `default_volume_tables.xml`; neither includes `a2dp_audio_policy_configuration.xml` or `bluetooth_qti_audio_policy_configuration.xml` (those two files are dead on this build).
* The live file **already declares**:
  ```xml
  <module name="a2dp" halVersion="2.0">
    <mixPorts><mixPort name="a2dp input" role="sink">PCM16 44100/48000 mono|stereo</mixPort></mixPorts>
    <devicePorts><devicePort tagName="BT A2DP In" type="AUDIO_DEVICE_IN_BLUETOOTH_A2DP" role="source"/></devicePorts>
    <routes><route type="mix" sink="a2dp input" sources="BT A2DP In"/></routes>
  </module>
  ```
  and `dumpsys media.audio_policy` shows it loaded as **HW Module 2 "a2dp", handle 18** with the route intact. The three `BT A2DP Out*` ports live in **module "primary"** (encodedFormats LDAC/aptX/aptX‑HD/AAC/SBC, 44.1–96 kHz) with routes from `primary output, deep_buffer, compressed_offload, voip_rx` — this is the split‑A2DP AFE‑proxy path.
* **Not wired / not happening:** `BT A2DP In` never appears under *Available input devices* — on SDK 30 `A2dpSinkService` does not tell AudioService about the sink link, so nothing calls `setDeviceConnectionState(AUDIO_DEVICE_IN_BLUETOOTH_A2DP, AVAILABLE)`. The primary module has **no** `BT A2DP In` port and no input route for it. `/vendor/lib*/hw` contains `audio.bluetooth.default.so`, `audio.bluetooth_qti.default.so`, `audio.primary.bengal.so`, `audio.r_submix.default.so`, `audio.usb.default.so` — there is no `audio.a2dp.default.so` visible in the listing, yet the module got a handle, so the HAL that backs "a2dp input" is opaque from these dumps.
* Therefore the **policy XML needs no structural patch for the declared route**. `post-fs-data.sh` is verify‑only by default (mounts nothing while the a2dp module/port/route are present), re‑inserts the module if a firmware update drops it, and offers the opt‑in experiment (`/data/adb/carrelay/a2dp_in_primary`) that moves `BT A2DP In` into `primary` + adds it to the `primary input` route so `audio.primary.bengal` owns the capture (the `hfp_audio` module already proves SLIM_7_TX capture works on this DSP). Any mounted copy carries the exact label seen on `/vendor/etc/audio/*`: `u:object_r:vendor_configs_file:s0`.
* What really decides Modes 2/3: whether `libbluetooth_qti.so` delivers decoded sink PCM to the HAL "a2dp input" or plays it as an AudioTrack (`BtifAvrcpAudioTrack`, uid 1002 — the AOSP R behaviour). Because that is not observable from the bundle, the relay publishes the input device itself (`A2dpInputPublisher`) and, when the HAL input stays silent, falls back to a **uid‑1002 dynamic AudioPolicy** (loop‑back mix for Oboe, render mix for Mode 3) which works with the AudioTrack path.

## 4. Property / coexistence findings

* `persist.bluetooth.maxconnectedaudiodevices` is the key `AdapterProperties` reads (the dump reports 5). The requested spelling `persist.bluetooth.maxconnected.audiodevices` is **not** read by the stack; the module sets the real key to 2.
* Offload codec caps are wide open (`…-aptxhd-aac-ldac`). With a second A2DP link (sink) sharing the 1×1 WCN3980, the module restricts the **car** link to `sbc-aac` and enables AAC VBR/frame control (`persist.vendor.qcom.bluetooth.aac_vbr_ctl.enabled`, `persist.vendor.bt.aac_frm_ctl.enabled` – the latter already true).
* SBC bitpool is a compile‑time constant of `libbluetooth(_qti)` (max 53); there is no public property for it on this build, so no fake key is shipped.
* `/system/etc/permissions` is already a Magisk tmpfs with `privapp-permissions-com.local.carhome.xml`, confirming the whitelist mount pattern works for a second priv‑app (`privapp-permissions-carrelay.xml`).
* Priv‑app native libraries: `/system/app/Bluetooth/lib/arm64/*.so` are symlinks — system apps are never extracted, so `CarRelay.apk` ships `extractNativeLibs=false` and the packager also drops `lib/arm64/*.so` next to it.

## 5. Summary of what each solution changes on the device

| | Solution 1 (module) | Solution 2 (app, Mode 2) | Solution 3 (app, Mode 3) |
|---|---|---|---|
| Bluetooth resources | `CarIvIBtOverlay` adds auto‑focus; keeps old overlay | — | — |
| Services | `pm enable` sink/CT/HF client/browser; `AvrcpTargetService` off | — | — |
| Audio policy | verify / re‑insert / opt‑in primary move, bind‑mount `vendor_configs_file` | publishes `BT A2DP In` at runtime | publishes `BT A2DP In` at runtime |
| Props | sink switch, max audio devices 2, car link `sbc-aac`, AAC VBR | — | — |
| PCM path | none (A2dpSinkStreamHandler → APM → car) | Oboe duplex (HAL in → car) or uid loop‑back → Oboe push | AudioFlinger PatchPanel bridge or uid render mix |
