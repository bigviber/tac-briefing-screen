#!/system/bin/sh
# Magisk module installer hook (sourced by module_installer.sh).
# Verifies we are on the firmware the bundle was captured on, keeps the
# existing btsink_overlay_prop / btdual_patch / carhome_priv / hfp_audio modules
# untouched, and sets ownership / modes / SELinux contexts for the payload.
SKIPUNZIP=0

ui_print "- Car IVI BT Relay for Lenovo TB-J606F"

MODEL=$(getprop ro.product.model)
INCR=$(getprop ro.build.version.incremental)
SDK=$(getprop ro.build.version.sdk)

if [ "$SDK" != "30" ]; then
  abort "! This module targets Android 11 (SDK 30). Found SDK $SDK."
fi
case "$MODEL" in
  *J606F*) ;;
  *) ui_print "! Warning: expected Lenovo TB-J606F, found '$MODEL'. Continuing." ;;
esac
case "$INCR" in
  *S320311_230810*) ui_print "- Firmware S320311_230810_ROW confirmed" ;;
  *) ui_print "! Firmware $INCR differs from the audited S320311_230810_ROW build; post-fs-data.sh will re-verify the audio policy." ;;
esac

for m in btsink_overlay_prop btdual_patch carhome_priv hfp_audio; do
  [ -d /data/adb/modules/$m ] && ui_print "- Existing module '$m' detected (left untouched)"
done

ui_print "- Setting permissions"
set_perm_recursive $MODPATH 0 0 0755 0644
set_perm $MODPATH/post-fs-data.sh 0 0 0755
set_perm $MODPATH/service.sh 0 0 0755
set_perm $MODPATH/uninstall.sh 0 0 0755

# Overlay and priv-app payload keep system_file so idmap/PackageManager treat
# them exactly like the stock /product/overlay and /system/priv-app entries.
[ -f $MODPATH/system/product/overlay/CarIvIBtOverlay.apk ] && \
  set_perm $MODPATH/system/product/overlay/CarIvIBtOverlay.apk 0 0 0644 u:object_r:system_file:s0
[ -d $MODPATH/system/priv-app/CarRelay ] && \
  set_perm_recursive $MODPATH/system/priv-app/CarRelay 0 0 0755 0644 u:object_r:system_file:s0
[ -f $MODPATH/system/etc/permissions/privapp-permissions-carrelay.xml ] && \
  set_perm $MODPATH/system/etc/permissions/privapp-permissions-carrelay.xml 0 0 0644 u:object_r:system_file:s0

mkdir -p /data/adb/carrelay

# ---------------------------------------------------------------------------
# Component states - applied ONCE here, not on every boot.
# pm enable/disable on com.android.bluetooth makes PackageManager kill the
# Bluetooth process; doing it from service.sh restarted the stack on the
# first boot after install. In Magisk app installs ($BOOTMODE=true) pm is
# available now, so pin the states while the user is watching the installer.
# Recovery installs ($BOOTMODE=false) cannot run pm; the manifest resource
# gates (flipped by the overlays) then decide, which is the stock behaviour.
# AvrcpTargetService is intentionally not touched: its resource gate
# (profile_supported_avrcp_target=false) already keeps it off.
# ---------------------------------------------------------------------------
BT=com.android.bluetooth
if [ "$BOOTMODE" = "true" ]; then
  ui_print "- Pinning sink-side Bluetooth services (one-time, Bluetooth will restart once)"
  for c in \
    a2dpsink.A2dpSinkService \
    avrcpcontroller.AvrcpControllerService \
    avrcpcontroller.BluetoothMediaBrowserService \
    hfpclient.HeadsetClientService \
    pbapclient.PbapClientService \
    pbapclient.AuthenticationService ; do
    pm enable --user 0 "$BT/.$c" >/dev/null 2>&1 && echo "$BT/.$c" >> /data/adb/carrelay/pinned_components
  done
  sort -u /data/adb/carrelay/pinned_components -o /data/adb/carrelay/pinned_components 2>/dev/null
else
  ui_print "- Recovery install: component states left to the overlay resource gates"
fi

ui_print "- Optional experiment flag: touch /data/adb/carrelay/a2dp_in_primary (see post-fs-data.sh)"
ui_print "- Before removing the module: tap 'Unpin roles' in Car Relay to restore per-device connection policies"
ui_print "- Done. Reboot to apply."
