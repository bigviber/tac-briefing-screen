#!/system/bin/sh
# ---------------------------------------------------------------------------
# Car IVI BT Relay - late_start service stage.
# Runs once the framework is up: enables the RRO overlays, forces the sink-side
# Bluetooth services on, relaxes the hidden-API policy for the companion app,
# grants its runtime permission and starts the relay foreground service.
# Every command is idempotent; re-running on each boot is intended.
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

# Wait for boot to complete, then give PackageManager / OverlayManager a moment.
until [ "$(getprop sys.boot_completed)" = "1" ]; do sleep 2; done
sleep 6
log_i "boot completed, applying runtime configuration"

# --- Hidden API --------------------------------------------------------------
# The companion is a /system/priv-app with android:usesNonSdkApi="true", which
# already lifts the restriction for that app. The global policy is relaxed as a
# belt-and-braces measure for adb/debug sessions (1 = allow all, log nothing).
settings put global hidden_api_policy 1
EXEMPT=$(settings get global hidden_api_blacklist_exemptions)
case "$EXEMPT" in
  *Landroid/bluetooth/*) ;;
  *) settings put global hidden_api_blacklist_exemptions "${EXEMPT:+$EXEMPT,}Landroid/bluetooth/,Landroid/media/" ;;
esac

# --- RRO overlays ------------------------------------------------------------
# Both overlays are static (android:isStatic="true") so OverlayManager enables
# them at install time; the explicit enable makes the state deterministic and
# covers the case where someone disabled one by hand.
cmd overlay enable --user 0 $OLD_OVERLAY >/dev/null 2>&1 && log_i "enabled $OLD_OVERLAY" || log_i "$OLD_OVERLAY not present (ok)"
cmd overlay enable --user 0 $NEW_OVERLAY >/dev/null 2>&1 && log_i "enabled $NEW_OVERLAY" || log_i "$NEW_OVERLAY enable failed/not present"
cmd overlay set-priority $NEW_OVERLAY highest >/dev/null 2>&1

for r in bool/profile_supported_a2dp_sink bool/profile_supported_avrcp_controller bool/profile_supported_hfpclient bool/a2dp_sink_automatically_request_audio_focus; do
  log_i "$r = $(cmd overlay lookup $BT $BT:$r 2>&1)"
done

# --- Sink-side Bluetooth services ------------------------------------------
# The service manifest entries are gated on @bool/profile_supported_* resources
# (A2dpSinkService -> 0x7f02000a, HeadsetClientService -> 0x7f02000f, ...).
# The overlay flips the booleans; pm enable pins the component state as well so
# a resource lookup race at Bluetooth start-up cannot leave them disabled.
for c in \
  a2dpsink.A2dpSinkService \
  avrcpcontroller.AvrcpControllerService \
  avrcpcontroller.BluetoothMediaBrowserService \
  hfpclient.HeadsetClientService \
  pbapclient.PbapClientService \
  pbapclient.AuthenticationService ; do
  pm enable --user 0 "$BT/.$c" >/dev/null 2>&1 && log_i "enabled $BT/.$c"
done

# Keep the AVRCP *target* (tablet-as-phone) off: the car is the AVRCP target of
# our source link and the iPhone is the target of our controller link. Running a
# local target as well confuses some head units' metadata display.
pm disable --user 0 "$BT/.avrcp.AvrcpTargetService" >/dev/null 2>&1

# --- Companion app --------------------------------------------------------------
if pm path $APP >/dev/null 2>&1; then
  # RECORD_AUDIO is a runtime permission: pre-grant it so the AudioPolicy
  # loop-back capture (Mode 2 fall-back) never blocks on a dialog in the car.
  pm grant $APP android.permission.RECORD_AUDIO >/dev/null 2>&1
  # Make sure the priv-app whitelist took: BLUETOOTH_PRIVILEGED must be granted.
  if dumpsys package $APP | grep -q 'android.permission.BLUETOOTH_PRIVILEGED: granted=true'; then
    log_i "$APP has BLUETOOTH_PRIVILEGED"
  else
    log_i "WARNING: $APP lacks BLUETOOTH_PRIVILEGED - check privapp-permissions-carrelay.xml mount"
  fi
  # Exempt from battery optimisation so the relay service survives Doze in the car.
  dumpsys deviceidle whitelist +$APP >/dev/null 2>&1
  am start-foreground-service --user 0 -n $APP/.service.RelayForegroundService >/dev/null 2>&1 \
    && log_i "started RelayForegroundService" \
    || log_i "could not start RelayForegroundService (will start on power/boot receiver)"
else
  log_i "$APP not installed yet"
fi

# --- Diagnostics snapshot for the dashboard ---------------------------------
{
  echo "== $(date)"
  getprop | grep -E 'persist\.(vendor\.)?(bluetooth|qcom\.bluetooth|bt|vendor\.service\.bt)'
  echo "-- overlay"; cmd overlay list | grep -A3 "^$BT"
  echo "-- bt services"; dumpsys bluetooth_manager | grep -E 'Profile: (A2dpService|A2dpSinkService|AvrcpControllerService|HeadsetClientService)|MaxConnectedAudioDevices|A2dpOffloadEnabled'
  echo "-- policy mount"; grep audio_policy_configuration /proc/mounts || echo "(stock policy in use)"
} > $WORK/boot_snapshot.txt 2>&1

log_i "done"
exit 0
