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

# Overlay and priv-app payload keep system_file so idmap/PackageManager treat
# them exactly like the stock /product/overlay and /system/priv-app entries.
[ -f $MODPATH/system/product/overlay/CarIvIBtOverlay.apk ] && \
  set_perm $MODPATH/system/product/overlay/CarIvIBtOverlay.apk 0 0 0644 u:object_r:system_file:s0
[ -d $MODPATH/system/priv-app/CarRelay ] && \
  set_perm_recursive $MODPATH/system/priv-app/CarRelay 0 0 0755 0644 u:object_r:system_file:s0
[ -f $MODPATH/system/etc/permissions/privapp-permissions-carrelay.xml ] && \
  set_perm $MODPATH/system/etc/permissions/privapp-permissions-carrelay.xml 0 0 0644 u:object_r:system_file:s0

mkdir -p /data/adb/carrelay
ui_print "- Optional experiment flag: touch /data/adb/carrelay/a2dp_in_primary (see post-fs-data.sh)"
ui_print "- Done. Reboot to apply."
