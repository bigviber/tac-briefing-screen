#!/system/bin/sh
# Read-only state snapshot for the CarRelay (uncle's dual-BT) tests. Prints to stdout.
echo "== $(date) serial=$(getprop ro.serialno)"
echo "-- modules"
for m in /data/adb/modules/*; do
  v=$(grep -E '^version=' $m/module.prop)
  f=""; [ -f $m/disable ] && f="$f DISABLED"; [ -f $m/remove ] && f="$f REMOVE"
  echo "$m $v$f"
done
echo "-- props"
getprop | grep -E 'maxconnectedaudio|a2dp\.sink|a2dp_offload|offload\.cap|aac_vbr|aac_frm|splita2dp|scram|twsp'
echo "-- settings"
echo "hidden_api_policy=$(settings get global hidden_api_policy)"
echo "exemptions=$(settings get global hidden_api_blacklist_exemptions)"
echo "-- overlays"; cmd overlay list com.android.bluetooth
echo "-- bt component states"
dumpsys package com.android.bluetooth | sed -n '/enabledComponents:/,/^ *[a-z]*Permissions:/p' | head -30
pm dump com.android.bluetooth | grep -E 'disabledComponents' -A6 | head -10
echo "-- carrelay"
pm path com.carivi.relay 2>&1; ls -la /data/adb/carrelay 2>&1
echo "-- policy mount"; grep audio_policy_configuration /proc/mounts || echo "(stock policy)"
echo "-- bt"
dumpsys bluetooth_manager | grep -E 'MaxConnectedAudioDevices|A2dpOffloadEnabled|mActiveDevice|Devices Tracked|ConnectionState|  [0-9A-F:]{17}' | head -30
echo "-- audio outputs"
dumpsys audio | grep -iE 'Devices:|A2DP' | head -15
