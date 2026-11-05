# HFP-client audio on the Lenovo Tab P11 (bengal): analysis and route design

Offline static analysis of `libhfp.so`, `audio.primary.bengal.so`, the vendor XMLs and the live
mixer control list (`tinymix_controls.txt`). Nothing here was run on the device.

Files in this folder:

| file | what |
|---|---|
| `mixer_paths.xml` | patched copy of `/vendor/etc/mixer_paths.xml` (only the hunks in `mixer_paths.diff` differ) |
| `audio_platform_info_intcodec.xml` | patched copy of `/vendor/etc/audio_platform_info_intcodec.xml` (`audio_platform_info_intcodec.diff`) |
| `hfp_legs.sh` | device-side helper (root): leg tests, downlink bridge, bind-mount/unmount |
| `tone_16k_stereo.wav` | 3 s, 440 Hz, 16 kHz stereo test tone for the playback legs |
| `libhfp_disasm.txt` | annotated Thumb-2 disassembly of libhfp.so (strings/PLT resolved) |
| `disasm.py`, `elfinfo.py`, `patch_xml.py` | the tools used (run with the `toolchain/pyre` venv, `python -I`) |

## 1. What libhfp.so actually does on this build (verified from the binary)

`start_hfp` is inlined into `hfp_set_parameters` (0x30e8..0x3378 in `libhfp_disasm.txt`). The
HAL hands libhfp a struct of 12 function pointers at `hfp_init` (order verified against the stores
in `hfp_init` and their later use): `platform_set_mic_mute, platform_get_pcm_device_id,
platform_set_echo_reference, select_devices, ext_hw_plugin_usecase_start/stop,
get_usecase_from_list, disable_audio_route, disable_snd_device, voice_get_mic_mute,
auto_hal_start_hfp_downlink, auto_hal_stop_hfp_downlink`.

Parameter keys handled by `hfp_set_parameters`, in this order: `hfp_enable` (true -> start_hfp,
false -> stop_hfp), `hfp_set_sampling_rate` (8000 -> ucid 19 `hfp-sco`, 16000 -> ucid 20
`hfp-sco-wb`; also sets `pcm_config_hfp.rate`), `hfp_route_spkr` (any value sets a sticky
"force speaker" flag, never cleared until HAL restart), `routing=<devmask>` (while HFP runs and
not forced: re-assigns the usecase and primary-output device lists and calls `select_devices`;
2 = speaker, 8 = wired headphone, 4 = wired headset, 0x20000 = line), `hfp_vol_mixer_ctl=<name>`
(overrides the volume mixer control, default `Internal HFP RX Volume`), `hfp_volume` (0..15 ->
0..0x2000 on that control), `hfp_pcm_dev_id`, `hfp_mic_volume` (0..15 -> `Playback <asm id>
Volume`).

Note: `hfp_enable` is evaluated **before** `hfp_set_sampling_rate` inside one call, so the two
keys must be sent in separate `setParameters()` calls (the BT stack already does this).

`start_hfp(adev)` sequence on this build:

1. if `adev->enable_hfp` already set -> "HFP is already active!" and return 0; else set it,
   `platform_set_mic_mute(platform, false)`.
2. `uc_info = calloc(0x4c)`; `id = hfpmod.ucid (19/20)`, `type = PCM_HFP_CALL (4)`,
   `stream.out = adev->primary_output`, device list copied from the primary output; if the
   force-speaker flag is set both device lists are rewritten to `AUDIO_DEVICE_OUT_SPEAKER`.
3. `list_add_tail(&adev->usecase_list)`, then `select_devices(adev, ucid)` -> this is what
   enables the snd devices (`voice-speaker-hfp` / `voice-speaker-mic-hfp` on speaker,
   `voice-headphones` + `handset-mic` on a 3.5 mm headphone, `voice-headset` + `voice-headset-mic`
   on a headset) and applies the use-case path `use_case_table[ucid]` + " " + backend tag
   (`hfp-sco`, `hfp-sco-wb`, `hfp-sco-wb headphones`, ...). logcat line to look for:
   `audio_hw_primary: enable_audio_route: apply mixer and update path: hfp-sco-wb`.
4. `ext_hw_plugin_usecase_start` (stub on this device).
5. `rx = platform_get_pcm_device_id(ucid, PCM_PLAYBACK)`, `tx = platform_get_pcm_device_id(ucid,
   PCM_CAPTURE)`, `asm = hfpmod.hfp_pcm_dev_id`. If any < 0 -> "Invalid PCM devices", -EIO.
   Otherwise logs `HFP PCM devices (rx: %d tx: %d pcm dev id: %d) usecase(%d)`.
6. `hfp_pcm_rx = pcm_open(card, rx, PCM_OUT, &pcm_config_hfp)`; `hfp_pcm_tx = pcm_open(card, tx,
   PCM_IN, ...)`; each `!pcm_is_ready` -> logs `start_hfp: <tinyalsa error>` and returns **-EIO
   (-5)**. Then `pcm_start(hfp_pcm_rx)`, `pcm_start(hfp_pcm_tx)` (failure -> -EINVAL, -22).
7. `auto_hal_start_hfp_downlink` (returns 0: no auto_hal library loaded), result only logged.
8. `hfp_sco_rx = pcm_open(card, asm, PCM_OUT)`, `hfp_sco_tx = pcm_open(card, asm, PCM_IN)`,
   same -EIO check, then `pcm_start` on both (-EINVAL on failure).
9. `is_hfp_running = 1`; `hfp_set_volume(hfp_volume)` (return ignored); `hfp_set_mic_mute(adev,
   adev->mic_muted)` -> reads/sets `Playback <asm> Volume` (15/15 -> 0x2000 = unity).
10. On any failure: `stop_hfp` (closes the 4 pcms, `disable_audio_route`, `disable_snd_device` x2,
    unmute, frees uc_info) and "Problem in HFP start: status(%d)".

`pcm_config_hfp` (`.data` 0x64a4): channels 1, rate 8000 (or 16000 after
`hfp_set_sampling_rate`), period_size 240, period_count 2, S16_LE, start_threshold 0,
stop_threshold INT_MAX, avail_min 0. `hfpmod` defaults: `hfp_pcm_dev_id = 24`, `ucid = 19`,
`mic_volume = 15.0`, `mic_mute = 0`, plus a 128-byte `hfp_vol_mixer_ctl` buffer.

The HAL's static `pcm_device_table` (platform.c, `.data` 0x71b74, 86 use cases) has for
`USECASE_AUDIO_HFP_SCO` (19) and `_WB` (20): **out = 5, in = 23**. The ODM XML overrides only
`in` to 12. So today: rx = 5 (`AFE-PROXY RX`, the RT-proxy playback device), tx = 12
(MultiMedia10 capture), asm = 39 (`Primary TDM0 Hostless Capture`, capture only).

### Root cause of status(-5)

-5 is -EIO, which this code returns only from a `pcm_open`/`pcm_is_ready` failure (the "Invalid
PCM devices" check cannot fire: 5, 12 and 39 are all >= 0; pcm_start failures give -22). The
first open that fails is `pcm_open(5, PCM_OUT)` or `pcm_open(12, PCM_IN)` with the 8 kHz / 240 x 2
config, and even if both passed, `pcm_open(39, PCM_OUT)` must fail because pcm 39 has no playback
substream (`/dev/snd/pcmC0D39p` does not exist). The exact message is in logcat at level E, tag
`audio_hw_hfp`, right before `stop_hfp: enter`: `start_hfp: cannot open device '/dev/snd/pcmC0D..'`
or `start_hfp: cannot set hw params: ...` (grep for `start_hfp:`). The three config values
(out 5 by default, in 12, asm 39) are all wrong for this board; the XML in this folder fixes them.

The "unable to find path 'voice-speaker-hfp' / 'voice-speaker-mic-hfp'" lines are a separate
defect (missing device paths, so the amps and the mic were never enabled) but by themselves do
not abort start_hfp (audio_route only warns).

## 2. What the DSP routing can and cannot do on this kernel (from tinymix_controls.txt)

Hard facts from the live control list (these are what matter, not mixer_paths.xml):

* The only `... Port Mixer SLIM_7_TX` (AFE loopback out of the BT TX port) controls are on
  `SLIMBUS_0_RX`, `SLIMBUS_6_RX`, `INT0_MI2S_RX`, `INT4_MI2S_RX`: none of them exists as a backend
  on this board. There is **no** `RX_CDC_DMA_RX_0/1 Port Mixer SLIM_7_TX` (the defaults block of
  mixer_paths.xml names them, but the kernel does not have them) and no `PRI_TDM_RX_0 Port Mixer
  SLIM_7_TX`.
* `SLIMBUS_7_RX` has no `Port Mixer` inputs at all, only `Audio Mixer MultiMediaN` (FE) inputs.
* FEs that can capture `SLIM_7_TX`: MultiMedia1, 2, 5, 8, 10, 17, 27 (all host/`msm-pcm-dsp` FEs)
  plus the voice FEs. The ASM loopback FE MultiMedia6 (pcm 33, the only hostless-capable FE with
  both directions and `Playback 33 Volume`) has **no** `SLIM_7_TX` input.
* MultiMedia6 does have `MultiMedia6 Mixer TX_CDC_DMA_TX_3 / TX_4` and `SLIMBUS_7_RX Audio Mixer
  MultiMedia6`, so the uplink (mic -> phone) can be a pure-DSP ASM loopback with SRC.
* Hostless switches that exist: `SCO_SLIM7_DL_HL`, `HFP_SLIM7_UL_HL`, `A2DP_SLIM7_UL_HL`,
  `HFP_INT_UL_HL`, `PRI_TDM_RX_0_DL_HL`, `RX_CDC_DMA_RX_0/1_DL_HL`.
* `BT SampleRate` (and `RX`/`TX` variants) sets the SLIMBUS_7 backend rate (KHZ_8/KHZ_16).

Conclusion (high confidence): **the downlink (caller's voice from SLIM_7_TX) cannot be moved to
PRI_TDM_RX_0 or to RX_CDC_DMA_RX_0 inside the DSP on this kernel.** Neither an AFE loopback nor an
ASM loopback FE has the required input/output pair. That is true for the speakers *and* for the
headphone jack. The reference Qualcomm design (`<RX> Port Mixer SLIM_7_TX`) was compiled out of
this ODM kernel. The only consumers of SLIM_7_TX are host FEs, so one user-space hop is required.

The uplink (tablet mic -> phone) *can* be done in the DSP, and that is what the patched config
makes libhfp do.

## 3. Route design

```
Uplink   (DSP only):  amic3 (TX DEC2) -> TX_CDC_DMA_TX_3 (48 kHz) -> [ADM SRC] -> MultiMedia6 ASM
                      loopback (pcm 33, 8/16 kHz mono, opened+started by libhfp) -> SLIMBUS_7_RX (BT)
Downlink (one hop):   SLIMBUS_7_TX (BT, 8/16 kHz) -> MultiMedia10 capture (pcm 12) -> user space
                      -> MultiMedia10 playback (pcm 12) -> [ADM SRC/upmix] -> PRI_TDM_RX_0 (speakers,
                      48 kHz 4ch S32) or RX_CDC_DMA_RX_0 (3.5 mm jack, 48 kHz)
Power/keep-alive:     SLIMBUS_7 Hostless (pcm 35) opened+started both ways by libhfp as rx/tx,
                      with SCO_SLIM7_DL_HL / HFP_SLIM7_UL_HL on, so both BT ports stay up.
```

Why these pcm ids: libhfp needs four distinct, startable streams. 33 OUT+IN is the loopback
session (starting both halves starts the ASM loopback); 35 OUT+IN is hostless (no data needed).
Nothing else is reserved: 12 stays free for the bridge, 23 (MultiMedia9) is unused on a tablet
and is the fallback bridge playback FE (`PRI_TDM_RX_0 Audio Mixer MultiMedia9` exists).

### Changes and rationale

`audio_platform_info_intcodec.xml`
* `USECASE_AUDIO_HFP_SCO` / `_WB`: `out=35, in=35` (was out=5 default, in=12). 5 is the AFE RT
  proxy playback device and cannot be a hostless keep-alive; 12 is needed by the bridge.
* `hfp_pcm_dev_id = 33` (was 39, capture-only). 33 = MultiMedia6 ASM loopback FE, which is the
  Qualcomm meaning of this parameter (`Playback <id> Volume` is then the uplink gain).

`mixer_paths.xml`
* defaults: add `SCO_SLIM7_DL_HL Switch`, `MultiMedia6 Mixer TX_CDC_DMA_TX_3/4` so audio_route
  resets them to 0 after a call (the two non-existent Port Mixer lines are left as they were; the
  HAL already skips them with "Control ... doesn't exist").
* `hfp-sco`: both hostless switches, the MM6 loopback input (mic) and output (SLIMBUS_7_RX), the
  bridge input (`MultiMedia10 Mixer SLIM_7_TX`) and bridge output to the speakers
  (`PRI_TDM_RX_0 Audio Mixer MultiMedia10`).
* `hfp-sco headphones` (backend tag of `SND_DEVICE_OUT_VOICE_HEADPHONES` and `_VOICE_LINE`) and
  `hfp-sco headset` (4-pole headset, mic on TX_CDC_DMA_TX_4): same, with the bridge output on
  `RX_CDC_DMA_RX_0 Audio Mixer MultiMedia10`.
* `hfp-sco-wb*`: `BT SampleRate = KHZ_16` then include the NB path (same pattern as
  `compress-voip-call bt-sco-wb`). Reset restores KHZ_8 from the defaults.
* device paths `voice-speaker-hfp` -> `speaker` (TDM 4ch/S32/48k + aw_dev_0..3 amps) and
  `voice-speaker-mic-hfp` -> `speaker-mic` (amic3). They are placed after `voice-speaker-mic`,
  which is after the paths they include (audio_route requires included paths to be defined first).
* An optional, commented-out echo reference (`AUDIO_REF_EC_UL6 MUX = PRI_TDM_RX_0`) for the MM6
  capture; only useful if the ACDB topology of the mic device contains AEC. Leave it off for the
  first tests.

Volume: `hfp_volume` from the phone (seen as 1/15 in the failing log) is left on
`Internal HFP RX Volume`, which has no effect on this route (harmless error in logcat). Do **not**
point `hfp_vol_mixer_ctl` at `Playback 12 Volume` unless you want the phone's VGS to scale the
bridge (VGS=1 would make it nearly silent). Uplink gain is `Playback 33 Volume` (unity by
default, set by libhfp from `hfp_mic_volume`, and 0 when the HAL mutes the mic).

## 4. Verification procedure (root, no phone call first)

Push the folder: `adb push fable /data/local/tmp/fable` and work in `adb shell`, `su`.
`S=/data/local/tmp/fable/hfp_legs.sh`.

Leg 0, inventory (no changes made):
```
sh $S info      # tinypcminfo for 12/23/33/35; AFE_LOOPBACK_TX Port options; tinyplay/tinycap usage
sh $S state     # the controls this design touches + /proc/asound stream states
```
Check in the `info` output how tinyplay names stdin (`-` or `--`) and tinycap stdout (`--`); fix
the `bridge-start` line in the script if it differs.

Leg A, speakers through the bridge playback FE (HAL idle):
```
sh $S spk-on    # = "speaker" path + PRI_TDM_RX_0 Audio Mixer MultiMedia10
sh $S play      # tinyplay tone_16k_stereo.wav -D 0 -d 12  -> tone on the speakers (16 kHz FE into a 48 kHz 4ch TDM port)
sh $S spk-off
```
If the tone comes from only some of the four speakers, retry with a mono file (`CH=1`) or the
other way round; the ADM channel mixer decides the 2->4 / 1->4 mapping.

Leg B, 3.5 mm jack (plug a headphone or the AUX cable):
```
sh $S hp-on     # = "headphones" codec path + RX_CDC_DMA_RX_0 Audio Mixer MultiMedia10
sh $S play
sh $S hp-off
```

Leg C, tablet mic through the bridge capture FE:
```
sh $S mic-on    # = amic3 path + MultiMedia10 Mixer TX_CDC_DMA_TX_3
sh $S rec       # 5 s to /data/local/tmp/fable/mic.wav ; adb pull it and listen
sh $S mic-off
```

Leg D, bridge dry run (hear your own voice via mic -> pcm12 capture -> pipe -> pcm12 playback -> speakers):
```
sh $S loop-dry  ;  speak  ;  sh $S loop-dry-off
```
This proves pcm 12 can be open in both directions at once, the pipe works, and gives the latency
feel (~100 ms). The DSP does 48k mic -> 16k FE and 16k FE -> 48k TDM.

Leg E, the HAL with the patched XMLs, still no call:
```
sh $S mount                                   # bind-mount both XMLs over /vendor/etc
setprop ctl.restart vendor.audio-hal          # name from: getprop | grep init.svc | grep audio
# wait for audioserver to reconnect, then:
logcat -d | grep -E 'Loading mixer|HFP_PCM_DEV_ID|unable to find|Control .* doesn.t exist'
```
Expect `Updating HFP_PCM_DEV_ID as 33` and no "unable to find path" later. Then, from the app
(Bluetooth on, phone connected or not; the BT SLIMbus ports open without an SCO link):
```
AudioManager.setParameters("hfp_set_sampling_rate=16000");   // separate call
AudioManager.setParameters("hfp_enable=true");               // separate call
```
and check:
```
logcat -d | grep -E 'audio_hw_hfp|apply mixer and update path|voice-speaker'
   -> "HFP PCM devices (rx: 35 tx: 35 pcm dev id: 33) usecase(20)"
   -> "apply mixer and update path: hfp-sco-wb"   (if a suffix such as "hfp-sco-wb <tag>" shows up,
      add a path with that exact name that includes "hfp-sco-wb")
   -> "start_hfp: exit: status(0)"
sh $S state   -> HFP_SLIM7_UL_HL/SCO_SLIM7_DL_HL On, MM6 mixers On, pcm33p/c and pcm35p/c RUNNING
dmesg | grep -iE 'afe|adm|routing|loopback|slim' | tail -40      # no errors expected
sh $S bridge-start ; sh $S state ; sh $S bridge-stop              # pcm12c/pcm12p RUNNING, silence is fine
AudioManager.setParameters("hfp_enable=false");  sh $S state      # everything back to Off/closed
```
If `start_hfp` fails here, the message before `stop_hfp: enter` says which pcm: `-5` = open/hw
params (try `in/out=30` "CDC_DMA Hostless" instead of 35 if 35 is busy with A2DP ABR feedback),
`-22` with "pcm start for hfp pcm rx failed" = the hostless start refused (then set
`USECASE_AUDIO_HFP_SCO out` to 23 and keep in=35; a `msm-pcm-dsp` capture starts without a reader).

Leg F, real call (phone paired, tablet as HF client):
1. Make or receive a call; when SCO comes up the BT stack sends `hfp_set_sampling_rate`,
   `hfp_enable=true`, `hfp_volume`. Confirm `start_hfp: exit: status(0)` in logcat.
2. `sh $S bridge-start` (RATE=8000 if the log said `hfp_set_sampling_rate=8000`). The caller
   should now be heard on the speakers (or on the jack if a headphone/AUX cable is plugged, or
   after `setParameters("routing=8")`). The caller should hear the tablet mic (MM6 loopback).
3. Hang up: the stack sends `hfp_enable=false`; run `sh $S bridge-stop` (or let it die on the
   pcm error) and verify `state` is clean.
4. Production: have the app start/stop the bridge on `BluetoothHeadsetClient`
   `ACTION_AUDIO_STATE_CHANGED` (STATE_AUDIO_CONNECTED / DISCONNECTED) via `su`, or better,
   read the pcm 12 capture (`tinycap -- ...`) through the `su` process' stdout and feed an
   `AudioTrack` (16 kHz, USAGE_VOICE_COMMUNICATION): then Android's policy chooses speaker /
   jack / A2DP automatically and STREAM_VOICE_CALL volume applies. The `hfp-sco` path still
   has to set `MultiMedia10 Mixer SLIM_7_TX`; the TDM/CDC output mixers for MM10 are then unused.

Rollback: `sh $S umount` and restart the HAL again; the originals are untouched.

## 5. Stretch: HFP downlink into an A2DP stream to the car

Not possible in the DSP, and not even as a host hop if A2DP is "split" (offloaded):
`SND_DEVICE_OUT_BT_A2DP` uses the same `SLIMBUS_7_RX` port at 48 kHz with the encoder in the
ADSP, while SCO needs `SLIMBUS_7_RX/TX` at 8/16 kHz; one AFE port cannot carry both, so split
A2DP and SCO cannot coexist at all on this platform. The realistic target is the speaker /
headphone-jack route above. The only way to combine a call with A2DP to the car is to force A2DP
to the host encoder (`persist.vendor.qcom.bluetooth.enable.splita2dp=false`, so the BT stack
encodes SBC and sends it over HCI, leaving SLIM7 to SCO) and play the downlink through an
`AudioTrack` as in Leg F step 4; the WCN3988 then has to hold two ACL links plus one eSCO, which
is feasible but tight (and note `USECASE_AUDIO_A2DP_ABR_FEEDBACK` also claims pcm 35 when ABR
codecs are used with split A2DP).

## 6. Confidence summary

Verified (binary / live control list): start_hfp order and error codes, pcm ids it uses today
(5/12/39) and their defaults (5/23/24), parameter keys, hfpmod and pcm_config layout, the
function-pointer order, the missing device paths, the absence of any SLIM_7_TX port mixer or
MM6 SLIM_7_TX input, the existence of every control used in the new paths.

Inferred (medium-high): that pcm 33 is `msm-pcm-loopback` (both directions, `Playback 33 Volume`
and no `Capture 33 Volume`, exactly like the reference MM6) and pcm 35 is `msm-pcm-hostless`
(no per-stream controls); that hostless/loopback playback starts without data (that is how the
reference FM/HFP code works); that `voice-speaker-hfp` gets no backend tag (check the "apply mixer
and update path" line); that the ADM 1->2 / 2->4 channel mapping puts audio on all four speakers.

Unknowns to confirm on the device: `tinyplay` stdin spelling, whether the 4 speakers all play
from a mono/stereo FE, latency of the pipe, and whether the ACDB has an AEC topology for the
HFP mic device (echo on speakerphone until then).
