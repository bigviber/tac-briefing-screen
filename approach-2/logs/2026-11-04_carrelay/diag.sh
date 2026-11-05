#!/system/bin/sh
# Read-only diagnostics after a CarRelay boot. Prints to stdout.
echo "== $(date) $(uptime)"
echo "-- boot id / bt pid"; cat /proc/sys/kernel/random/boot_id; pidof com.android.bluetooth
echo "-- tombstones (newest first)"
for t in $(ls -t /data/tombstones/tombstone_* 2>/dev/null | head -4); do
  echo "== $t $(stat -c %y $t)"; grep -m4 -E '^pid:|^Abort message|^signal|^Cmdline' $t
done
echo "-- bt deaths / crashes in logcat"
logcat -d -b all | grep -E 'am_proc_died.*bluetooth|Process com.android.bluetooth .*has died|Fatal signal.*(bluetooth|bt_)|>>> com.android.bluetooth|am_crash|FATAL EXCEPTION|BluetoothManagerService: (Bluetooth|Error|Unable)|Bluetooth.*(crash|CRASH)' | head -40
echo "-- carrelay logs"
cat /data/adb/carrelay/post-fs-data.log 2>&1; echo; cat /data/adb/carrelay/service.log 2>&1
