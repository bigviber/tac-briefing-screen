#!/bin/sh
# Undoes reapply.sh: back to the stock Lenovo experience (stock launcher, gesture navigation, all packages enabled, default settings).
# Run from devices/lenovo-tab-p11 in Git Bash with the tablet on USB, then reboot. Line-by-line history: rollback_history_2026-11-02.sh.
ADB="${ADB:-../../adb.exe -s TABLET_SERIAL}"
[ "$($ADB shell getprop ro.serialno | tr -d '\r')" = "TABLET_SERIAL" ] || { echo "wrong or missing device"; exit 1; }
$ADB shell pm enable com.tblenovo.launcher
$ADB shell cmd package set-home-activity com.tblenovo.launcher/com.android.searchlauncher.SearchLauncher
$ADB shell cmd overlay enable-exclusive --category com.android.internal.systemui.navbar.gestural
for p in $(cat debloat_verified.txt | tr -d '\r'); do $ADB shell pm enable "$p" >/dev/null 2>&1; done
$ADB shell settings delete global hidden_api_blacklist_exemptions
$ADB shell settings put global force_resizable_activities 0
for k in window_animation_scale transition_animation_scale animator_duration_scale; do $ADB shell settings put global $k 1; done
$ADB shell settings put global wifi_scan_always_enabled 1
$ADB shell settings put global ble_scan_always_enabled 1
$ADB shell locksettings set-disabled false
# (Only Car Home v1.7 -> v1.6.2: $ADB shell pm uninstall-system-updates com.local.carhome; the module keeps the 1.6.2 system copy. Untested.)
$ADB uninstall com.local.carhome
# (Only Car Home v1.4 -> v1.3, not to stock: magisk --install-module carhome/carhome_priv-v1.3.zip (pushed to /data/local/tmp), reboot, then adb uninstall com.local.carhome (removes the v1.4 update, leaves the v1.3 system copy).)
$ADB uninstall com.local.adbkeeper
$ADB shell settings put global battery_protected_charge 0
echo "done: reboot the tablet"
# Dual A2DP patch (2026-11-03): ADB shell su -c 'touch /data/adb/modules/btdual_patch/remove' then reboot -> back to receive-only.
# Call-audio module (2026-11-03): su -c 'touch /data/adb/modules/hfp_audio/remove' then reboot -> stock audio configs.
# A2DP source device limit (2026-11-03): su -c 'resetprop -p --delete persist.bluetooth.maxconnectedaudiodevices'
# CarRelay test module (2026-11-04, uncle's dual-BT solutions): $ADB shell su -c 'touch /data/adb/modules/carrelay_ivi/remove; settings delete global hidden_api_policy; pm default-state com.android.bluetooth/.avrcp.AvrcpTargetService; dumpsys deviceidle whitelist -com.carivi.relay; rm -r /data/adb/carrelay' then reboot.
# CarRelay v1.0.1 (2026-11-04 01:40): has uninstall.sh: in the dashboard tap Unpin roles first (if roles were pinned), then $ADB shell su -c "touch /data/adb/modules/carrelay_ivi/remove" and reboot; its one-shot service.d cleanup runs after that boot.
