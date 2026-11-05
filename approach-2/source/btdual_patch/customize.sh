# The stack library must keep its original SELinux label, or the Bluetooth app cannot load it.
set_perm $MODPATH/system/system_ext/lib64/libbluetooth_qti.so 0 0 0644 u:object_r:system_lib_file:s0
