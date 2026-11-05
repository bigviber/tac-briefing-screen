#!/bin/sh
# Rebuilds the verified, reboot-tested state of 2026-11-02 on a freshly reset Lenovo Tab P11 (TB-J606F), Android 11 (S320311 or later).
# Safe to run more than once. Run from devices/lenovo-tab-p11 in Git Bash, with the tablet on USB:
#   sh reapply.sh            then   python -I reboot_check.py 150
# Before running: finish the setup wizard (skip the Google account if possible), turn on Developer options and USB debugging,
# accept this PC's USB debugging prompt. Not restored by this script: Google sign-in, offline maps, Wi-Fi passwords, Bluetooth pairings,
# Car Home's own choices (pinned apps, nav app, volume lock).
# History of how this state was reached (including two boot-loop experiments) is in reapply_history_2026-11-02.sh: DO NOT RUN that file.
ADB="${ADB:-../../adb.exe -s TABLET_SERIAL}"
[ "$($ADB shell getprop ro.serialno | tr -d '\r')" = "TABLET_SERIAL" ] || { echo "wrong or missing device"; exit 1; }

echo "== 1/6 system settings"
$ADB shell locksettings set-disabled true                          # no lock screen
$ADB shell settings put secure immersive_mode_confirmations confirmed   # no 'viewing full screen' notice
$ADB shell settings put global window_animation_scale 0.5
$ADB shell settings put global transition_animation_scale 0.5
$ADB shell settings put global animator_duration_scale 0.5
$ADB shell settings put global wifi_scan_always_enabled 0          # car uses real GPS, no background scanning
$ADB shell settings put global ble_scan_always_enabled 0
$ADB shell cmd uimode night yes                                    # system dark mode
$ADB shell settings put global battery_protected_charge 0            # user's choice (2026-11-03): charge to 100%, Lenovo battery protection OFF
$ADB shell settings put global enable_freeform_support 1           # CarPlay-style app windows (Lenovo default is already 1)
$ADB shell settings put global force_resizable_activities 1
$ADB shell "settings put global hidden_api_blacklist_exemptions 'Landroid/app/ActivityOptions;->setLaunchWindowingMode,Landroid/bluetooth/'"

echo "== 2/6 Car Home (our launcher) as HOME"
$ADB install -r apks/carhome-v1.7.apk
$ADB shell pm grant com.local.carhome android.permission.READ_CONTACTS   # caller names (1.5)
$ADB shell pm grant com.local.carhome android.permission.ACCESS_FINE_LOCATION
$ADB shell pm grant com.local.carhome android.permission.ACCESS_COARSE_LOCATION
$ADB shell cmd notification allow_listener com.local.carhome/com.local.carhome.MediaListener
$ADB shell cmd package compile -m speed -f com.local.carhome
$ADB shell cmd package set-home-activity com.local.carhome/.MainActivity

echo "== 3/6 3-button navigation (gesture navigation needs the stock launcher)"
$ADB shell cmd overlay enable-exclusive --category com.android.internal.systemui.navbar.threebutton

echo "== 4/6 debloat: the 95 reboot-verified packages in debloat_verified.txt"
for p in $(cat debloat_verified.txt | tr -d '\r'); do
    $ADB shell pm disable-user --user 0 "$p" >/dev/null 2>&1 && echo "  off $p" || echo "  (skipped $p: not on this build)"
done

echo "== 5/6 stock launcher off (only after Car Home is HOME)"
$ADB shell pm disable-user --user 0 com.tblenovo.launcher

echo "== 6/6 guard: packages that must stay ENABLED (disabling them caused boot loops or a frozen Settings page)"
for p in com.android.phone com.android.server.telecom com.android.providers.telephony com.android.carrierconfig \
         com.android.providers.blockednumber com.lenovo.ue.device com.lenovo.tbengine com.android.printspooler \
         org.codeaurora.ims com.qualcomm.qcrilmsgtunnel com.qualcomm.atfwd com.android.ons com.android.smspush \
         com.qti.qualcomm.datastatusnotification com.google.android.cellbroadcastservice com.qualcomm.qti.uceShimService com.qti.dpmserviceapp; do
    $ADB shell pm enable "$p" >/dev/null 2>&1
done
echo "done: now run  python -I reboot_check.py 150"
# (Test only, 2026-11-04, not part of the restore) CarRelay dual-BT module: $ADB push carrelay_test/carrelay_ivi-v1.0.0-test1.zip /data/local/tmp/ && $ADB shell su -c 'magisk --install-module /data/local/tmp/carrelay_ivi-v1.0.0-test1.zip' then reboot (needs btdual_patch v5).
# (Test only, 2026-11-04 01:40) CarRelay v1.0.1: $ADB push carrelay_test/carrelay_ivi-v1.0.1.zip /data/local/tmp/ && $ADB shell su -c "magisk --install-module /data/local/tmp/carrelay_ivi-v1.0.1.zip" then reboot (needs btdual_patch v5).
# (Test only, 2026-11-04 02:25) CarRelay v1.0.2: $ADB push carrelay_test/carrelay_ivi-v1.0.2.zip /data/local/tmp/ && $ADB shell su -c "magisk --install-module /data/local/tmp/carrelay_ivi-v1.0.2.zip" then reboot (needs btdual_patch v5).
