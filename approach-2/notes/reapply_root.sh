#!/bin/sh
# Re-creates the ROOT-level additions on top of reapply.sh: dual-role Bluetooth (receive from a phone AND send to the car).
# Prerequisites: bootloader unlocked, Magisk v30.7 installed in the active boot slot (see CLAUDE.md "ROOTED"),
# Magisk > Superuser > Shell allowed. Run from devices/lenovo-tab-p11 in Git Bash with the tablet on USB.
# One manual step remains: in the LSPosed manager, enable 'BT Dual Audio' (its scope com.android.bluetooth is pre-selected).
export MSYS_NO_PATHCONV=1
ADB="${ADB:-../../adb.exe -s TABLET_SERIAL}"
SU() { $ADB shell "su -c '$1'"; }
[ "$($ADB shell getprop ro.serialno | tr -d '\r')" = "TABLET_SERIAL" ] || { echo "wrong or missing device"; exit 1; }

# REMOVED 2026-11-03: LSPosed + Zygisk made system_server crash (malformed binder calls from lspd); the dual-role hook they carried is gone too. Do not re-enable.
# echo "== Zygisk on (needed by LSPosed)"
# $ADB shell "su -c \"magisk --sqlite \\\"REPLACE INTO settings (key,value) VALUES('zygisk',1);\\\"\""

# echo "== LSPosed v1.9.2 (official release, sha256 0ebc6bcb...29622)"
# $ADB push ../../toolchain/dl/lsposed/LSPosed-v1.9.2-7024-zygisk-release.zip /data/local/tmp/lsposed.zip >/dev/null
# SU "magisk --install-module /data/local/tmp/lsposed.zip"

echo "== Bluetooth sink overlay + property module (btsink/module-B)"
$ADB push btsink/module-B-btsink_overlay_prop-v2.zip /data/local/tmp/module-B.zip >/dev/null
SU "magisk --install-module /data/local/tmp/module-B.zip"

echo "== receiver components (disabled in the firmware manifest)"
SU "pm enable com.android.bluetooth/.a2dpsink.A2dpSinkService"
SU "pm enable com.android.bluetooth/.avrcpcontroller.BluetoothMediaBrowserService"
# hands-free client (calls) and phonebook client: their overlay switches are in module B v2; without these three Bluetooth crash-loops
SU "pm enable com.android.bluetooth/.hfpclient.HeadsetClientService"
SU "pm enable com.android.bluetooth/.pbapclient.PbapClientService"
SU "pm enable com.android.bluetooth/.pbapclient.AuthenticationService"

echo "== sink role switch, stored in the persistent property store (module B's system.prop alone did not stick at boot)"
SU "resetprop -p persist.vendor.service.bt.a2dp.sink true"

echo "== our hook module (keeps the A2DP source when the sink is on)"
# DISABLED 2026-11-03: the dual role crashes Bluetooth natively as soon as a phone streams (see ledger 02:56); receive-only mode instead.
# $ADB install -r apks/btdual-v1.0.apk

echo "== Car Home as privileged app (BLUETOOTH_PRIVILEGED; the device enforces the privapp whitelist)"
$ADB push carhome/carhome_priv-v1.6.2.zip /data/local/tmp/carhome_priv.zip >/dev/null
SU "magisk --install-module /data/local/tmp/carhome_priv.zip"

# echo "done. Reboot, open the LSPosed manager:"
# echo "  $ADB shell am start -c org.lsposed.manager.LAUNCH_MANAGER com.android.shell/.BugreportWarningActivity"
echo "enable 'BT Dual Audio' (scope: Bluetooth), reboot again, then check:"
echo "  $ADB shell dumpsys bluetooth_manager | grep -E 'Profile: A2dp'    (expect A2dpService and A2dpSinkService)"

echo "== dual A2DP patch module (patched Qualcomm stack + Bluetooth.apk; build S320311 only)"
# (dual A2DP retired 2026-11-03, see ledger 11:40) $ADB push btdual_patch-v3.zip /data/local/tmp/btdual_patch.zip >/dev/null
# (dual A2DP retired 2026-11-03, see ledger 11:40) SU "magisk --install-module /data/local/tmp/btdual_patch.zip"

echo "== call-audio module (HFP client routes + bridge)"
$ADB push hfp_audio-v1.zip /data/local/tmp/hfp_audio.zip >/dev/null
SU "magisk --install-module /data/local/tmp/hfp_audio.zip"
# (dual A2DP retired 2026-11-03, see ledger 11:40) SU "resetprop -p persist.bluetooth.maxconnectedaudiodevices 1"   # dual A2DP: leave stream slots for the sink role
