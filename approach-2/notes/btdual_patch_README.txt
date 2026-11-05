Built 2026-11-03 from analysis/bluetooth/fable/ (patch_native.py, patch_dex_apk.py; both verify the original bytes).
Originals (device sha256): Bluetooth.apk 3599b566cedc327c..., libbluetooth_qti.so 16ecec109c0cb624... (build S320311). Only valid for that build.
Native: 0x1c11a8 bl btif_av_init -> bl 0xf5118 (cave: btif_av_init(w0); btif_a2dp_sink_startup(); return first result).
Dex: Config.addAudioProfiles gates at dex 0x179250 / 0x179268 -> if-eq v1,v1 (both profiles always added).
oat/.replace hides the stale odex/vdex.
v2: + 0x1b6be4 strb (remote-start flag) -> b 0xf5138 (CAVE2: if btif_av_is_sink_enabled() skip flag + honor_remote_start, else original);
    + 0x1a4f70 (btif_a2dp_sink_update_decoder entry) -> b 0xf5168 (CAVE3: rx_flush byte 0x3f3be8 = 0, then original sub sp).
    Root cause: this build sets peer_sep for every peer from persist.vendor.service.bt.a2dp.sink, so a phone's START ran the source remote-start path.
v3: + CAVE4 @0xf517c called from CAVE1: if btif_a2dp_source_media_task_is_running() (dual mode), repeat btif_av_sink_execute_service's register loop (BTA_AvRegister(0x40, "Advanced Audio Sink", 0, &bte_av_sink_media_callback, 0x110b) x max sink connections) without its BTA_AvEnable. Root cause: the sink registration lives in btif_av_init's first-role-only block, so no tsep=1 SEPs existed in dual mode. CAVE2/3 moved to 0xf513c/0xf516c.
v5 (stage A, patched5a.so sha1 da7e2e993afc): v1 + v2 + dual-capable registration (no v3 CAVE4): bta_av_api_register's source branch loops codec 0-9 (0x128da4 mov w21,#0xa; 0x128da8 b 0x128de0) and CAVE5B (hook 0x128e68) sets cs.tsep [sp,#0xfc] / cs.p_data_cback [sp,#0xe8] per index: >=7 -> 1 / bta_av_sink_data_cback, <7 -> 0 / 0 (stock values; the struct is zeroed at 0x128838-0x128868).
