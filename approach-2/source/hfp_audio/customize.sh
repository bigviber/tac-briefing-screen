# The audio HAL only reads vendor config files with their original label.
set_perm $MODPATH/system/vendor/etc/mixer_paths.xml 0 0 0644 u:object_r:vendor_configs_file:s0
set_perm $MODPATH/system/vendor/etc/audio_platform_info_intcodec.xml 0 0 0644 u:object_r:vendor_configs_file:s0
set_perm $MODPATH/bin/hfp_bridge.sh 0 0 0755
