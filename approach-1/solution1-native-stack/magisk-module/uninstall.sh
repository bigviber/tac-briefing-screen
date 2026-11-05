#!/system/bin/sh
# ---------------------------------------------------------------------------
# Car IVI BT Relay - uninstall hook.
#
# Magisk runs uninstall.sh at the next boot *before* the framework is up, so
# `pm`, `settings` and `dumpsys` are not usable here. We therefore drop a
# one-shot /data/adb/service.d script that performs the cleanup once
# sys.boot_completed=1 and then deletes itself.
#
# What gets undone:
#   * global hidden_api_policy=1 set by v1.0.0's service.sh (only if it is
#     still exactly "1"; newer versions never set it)
#   * the ",Landroid/media/" entry v1.0.0 appended to hidden_api_blacklist_exemptions
#     (the pre-existing "Landroid/bluetooth/" entry belonged to the user and stays)
#   * com.android.bluetooth/.avrcp.AvrcpTargetService forced to "disabled" by
#     v1.0.0 -> back to default-state (resource gate decides)
#   * deviceidle whitelist entry for com.carivi.relay
#   * /data/adb/carrelay (logs, pinned_components, experiment flag)
# What is deliberately kept:
#   * A2dpSinkService / AvrcpControllerService / BluetoothMediaBrowserService /
#     HeadsetClientService / PbapClientService component states - they were
#     enabled before this module existed (PackageManager enabledComponents in
#     the device dump) and still serve the btsink_overlay_prop setup.
#   * per-device connection policies: there is no shell API for them on SDK 30.
#     Use "Unpin roles" in the Car Relay dashboard before removing the module.
# ---------------------------------------------------------------------------
CLEANUP=/data/adb/service.d/carrelay_cleanup.sh
mkdir -p /data/adb/service.d

cat > "$CLEANUP" <<'EOF'
#!/system/bin/sh
TAG=CarRelay-cleanup
APP=com.carivi.relay
BT=com.android.bluetooth
until [ "$(getprop sys.boot_completed)" = "1" ]; do sleep 2; done
sleep 8
log -p i -t $TAG "one-shot cleanup start"

# hidden-API policy (v1.0.0 set it to 1 globally)
if [ "$(settings get global hidden_api_policy 2>/dev/null)" = "1" ]; then
  settings delete global hidden_api_policy
  log -p i -t $TAG "removed global hidden_api_policy=1"
fi
EX=$(settings get global hidden_api_blacklist_exemptions 2>/dev/null)
case "$EX" in
  *",Landroid/media/"*)
    NEWEX=$(echo "$EX" | sed 's#,Landroid/media/##')
    settings put global hidden_api_blacklist_exemptions "$NEWEX"
    log -p i -t $TAG "removed Landroid/media/ exemption"
    ;;
esac

# AvrcpTargetService was force-disabled by v1.0.0; resource gate takes over again.
pm default-state --user 0 "$BT/.avrcp.AvrcpTargetService" >/dev/null 2>&1

# Doze whitelist entry (ignored if the package is already gone)
dumpsys deviceidle whitelist -$APP >/dev/null 2>&1

rm -rf /data/adb/carrelay
log -p i -t $TAG "cleanup done, removing self"
rm -f "$0"
exit 0
EOF
chmod 0755 "$CLEANUP"
exit 0
