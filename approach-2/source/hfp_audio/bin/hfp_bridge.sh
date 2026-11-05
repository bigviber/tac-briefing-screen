#!/system/bin/sh
# Caller's voice for calls through the tablet. This kernel has no DSP route from the Bluetooth voice port (SLIM_7_TX) to the
# speakers or the headphone jack, so while libhfp has a call's audio up (it opens pcm 35, "SLIMBUS_7 Hostless"), the voice is
# captured on MultiMedia10 (pcm 12) and played back through MultiMedia10; the HAL's hfp-sco path routes both halves.
# The tablet's tinycap/tinyplay are old (file paths only, no pipes), so a FIFO pair carries the stream: tinycap -> cap fifo ->
# cat (prepends a WAV header) -> play fifo -> tinyplay. Polls /proc a few times a second; costs next to nothing.
D=${0%/*}; W=/dev/hfp_bridge; LOG=$W/log
mkdir -p $W; : > $LOG
log() { echo "$(date +%H:%M:%S) $*" >> $LOG; }
running() { grep -q RUNNING /proc/asound/card0/pcm35p/sub0/status 2>/dev/null; }
rate() { r=$(grep -m1 '^rate:' /proc/asound/card0/pcm35p/sub0/hw_params 2>/dev/null | grep -oE '[0-9]+' | head -1); [ "$r" = 8000 ] && echo 8000 || echo 16000; }
start() {
  R=$(rate); rm -f $W/cap $W/play; mkfifo $W/cap $W/play
  tinyplay $W/play -D 0 -d 12 -p 240 -n 4 >> $LOG 2>&1 &
  ( cat $D/hdr_$R.bin; cat $W/cap ) > $W/play &
  tinycap $W/cap -D 0 -d 12 -c 2 -r $R -b 16 -p 240 -n 4 >> $LOG 2>&1 &
  log "bridge on ($R Hz)"
}
stop() { pkill -f "tinycap $W/cap"; pkill -f "tinyplay $W/play"; pkill -f "cat $W/cap"; rm -f $W/cap $W/play; log "bridge off"; }
log "watcher up (pid $$)"
ON=0
while true; do
  if running; then [ $ON = 0 ] && { start; ON=1; }
  else [ $ON = 1 ] && { stop; ON=0; }
  fi
  sleep 0.3
done
