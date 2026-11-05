#!/system/bin/sh
# Starts the call-audio bridge watcher once the system is up (Magisk late_start service).
MODDIR=${0%/*}
( sleep 20; exec sh $MODDIR/bin/hfp_bridge.sh ) >/dev/null 2>&1 &
