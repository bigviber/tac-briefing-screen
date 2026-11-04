#!/system/bin/sh
# ---------------------------------------------------------------------------
# Car IVI BT Relay - late_start service stage (v1.0.1).
#
# Deliberately does NOT touch package component states, overlay state or the
# hidden-API policy any more:
#   * pm enable/disable on com.android.bluetooth makes PackageManager kill the
#     Bluetooth process ("Killing ... change com.android.bluetooth"), which on
#     the first boot after install restarted the stack. Component states are
#     now applied once, at install time, by customize.sh ($BOOTMODE).
#   * Both overlays (com.local.btsink.overlay, com.carivi.bt.overlay) are static
#     RROs; OverlayManager enables them itself and refuses enable/set-priority
#     on them. We only read `cmd overlay list`.
#   * The companion is a /system/priv-app with android:usesNonSdkApi="true",
#     which already exempts it from the hidden-API denylist; the global policy
#     is left at the firmware default.
# What remains here is read-only verification plus starting the relay service.
# ---------------------------------------------------------------------------
MODDIR=${0%/*}
TAG=CarRelay-svc
WORK=/data/adb/carrelay
APP=com.carivi.relay
BT=com.android.bluetooth
NEW_OVERLAY=com.carivi.bt.overlay
OLD_OVERLAY=com.local.btsink.overlay

mkdir -p $WORK
log_i() { log -p i -t $TAG "$1"; echo "[$TAG] $1" >> $WORK/service.log; }
: > $WORK/service.log

until [ "$(getprop sys.boot_completed)" = "1" ]; do sleep 2; done
sleep 6
log_i "boot completed, verifying (read-only)"

# --- Overlays: static -> only report ---------------------------------------
OVL=$(cmd overlay list 2>/dev/null | awk -v bt="$BT" 'BEGIN{p=0} $0==bt{p=1;next} /^$/{p=0} p==1{print}')
for o in $OLD_OVERLAY $NEW_OVERLAY; do
  case "$OVL" in
    *"[x] $o"*) log_i "overlay $o: enabled" ;;
    *"[ ] $o"*) log_i "overlay $o: present but DISABLED" ;;
    *)          log_i "overlay $o: not listed for $BT" ;;
  esac
done
for r in bool/profile_supported_a2dp_sink bool/profile_supported_avrcp_controller bool/profile_supported_hfpclient bool/a2dp_sink_automatically_request_audio_focus; do
  log_i "$r = $(cmd overlay lookup $BT $BT:$r 2>&1)"
done

# --- Sink-side services: report only (states were pinned by customize.sh) ---
for c in a2dpsink.A2dpSinkService avrcpcontroller.AvrcpControllerService avrcpcontroller.BluetoothMediaBrowserService hfpclient.HeadsetClientService; do
  if dumpsys package $BT 2>/dev/null | grep -A12 'enabledComponents:' | grep -q "$BT\.$c"; then
    log_i "$c: enabled (pinned)"
  else
    log_i "$c: not pinned (resource gate decides) - reinstall module to pin"
  fi
done

# --- Companion app --------------------------------------------------------------
if pm path $APP >/dev/null 2>&1; then
  # RECORD_AUDIO is a runtime permission; granting is idempotent and does not
  # restart anything. Needed for the Mode 2 uid loop-back ingress.
  pm grant $APP android.permission.RECORD_AUDIO >/dev/null 2>&1
  if dumpsys package $APP | grep -q 'android.permission.BLUETOOTH_PRIVILEGED: granted=true'; then
    log_i "$APP has BLUETOOTH_PRIVILEGED"
  else
    log_i "WARNING: $APP lacks BLUETOOTH_PRIVILEGED - check privapp-permissions-carrelay.xml mount"
  fi
  dumpsys deviceidle whitelist +$APP >/dev/null 2>&1
  am start-foreground-service --user 0 -n $APP/.service.RelayForegroundService >/dev/null 2>&1 \
    && log_i "started RelayForegroundService" \
    || log_i "could not start RelayForegroundService (power/boot receiver will)"
else
  log_i "$APP not installed"
fi

{
  echo "== $(date)"
  getprop | grep -E 'persist\.(vendor\.)?(bluetooth|qcom\.bluetooth|bt|vendor\.service\.bt)'
  echo "-- overlay"; echo "$OVL"
  echo "-- bt services"; dumpsys bluetooth_manager | grep -E 'Profile: (A2dpService|A2dpSinkService|AvrcpControllerService|HeadsetClientService)|MaxConnectedAudioDevices|A2dpOffloadEnabled'
  echo "-- policy mount"; grep audio_policy_configuration /proc/mounts || echo "(stock policy in use)"
} > $WORK/boot_snapshot.txt 2>&1

log_i "done"
exit 0
