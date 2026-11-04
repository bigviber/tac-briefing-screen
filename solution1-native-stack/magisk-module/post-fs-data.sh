#!/system/bin/sh
# ---------------------------------------------------------------------------
# Car IVI BT Relay - post-fs-data stage (runs before zygote / audioserver).
#
# Job: make sure AUDIO_DEVICE_IN_BLUETOOTH_A2DP ("BT A2DP In" -> "a2dp input")
# is wired into the audio policy that audioserver will load, and if a patched
# copy is needed, bind-mount it over the vendor file with the exact SELinux
# label the dump shows on /vendor/etc/audio/*: u:object_r:vendor_configs_file:s0
#
# Ground truth from j606f_bt_context_bundle.md (S320311_230810_ROW):
#   * APM "Config source: /vendor/etc/audio/audio_policy_configuration.xml"
#   * That file ALREADY declares  <module name="a2dp">  with mixPort
#     "a2dp input" (sink), devicePort "BT A2DP In" (AUDIO_DEVICE_IN_BLUETOOTH_A2DP)
#     and the route  a2dp input <- BT A2DP In   (HW Module 2, handle 18 in dumpsys).
#   * sha1 of the stock file: b3f5f0370da8a4480781741c6576862650bba0c4
#
# Therefore the default behaviour is VERIFY ONLY: when the live file still
# carries the a2dp module the script mounts nothing and logs that fact.
# A patched copy is produced and mounted only when:
#   (a) a firmware update removed the a2dp module / BT A2DP In port, or
#   (b) the experiment flag /data/adb/carrelay/a2dp_in_primary exists, which
#       moves "BT A2DP In" into the primary module and adds it as a source of
#       the "primary input" route (lets audio.primary.bengal / QTI a2dp.c own
#       the capture path instead of the generic a2dp HAL module).
# Every transformation is validated before anything is mounted; on any doubt
# the stock file is left untouched. Nothing here is destructive.
# ---------------------------------------------------------------------------
MODDIR=${0%/*}
TAG=CarRelay-pfd
SRC=/vendor/etc/audio/audio_policy_configuration.xml
WORK=/data/adb/carrelay
OUT=$WORK/audio_policy_configuration.xml
STOCK_SHA1=b3f5f0370da8a4480781741c6576862650bba0c4
VENDOR_CTX=u:object_r:vendor_configs_file:s0

log_i() { log -p i -t $TAG "$1"; echo "[$TAG] $1" >> $WORK/post-fs-data.log; }
log_w() { log -p w -t $TAG "$1"; echo "[$TAG] WARN $1" >> $WORK/post-fs-data.log; }

mkdir -p $WORK
: > $WORK/post-fs-data.log
log_i "start $(date)"

if [ ! -f "$SRC" ]; then
  log_w "$SRC missing - nothing to do"
  exit 0
fi

LIVE_SHA1=$(sha1sum "$SRC" | cut -d' ' -f1)
log_i "live sha1=$LIVE_SHA1 (stock=$STOCK_SHA1)"

HAS_MODULE=$(grep -c '<module name="a2dp"' "$SRC")
HAS_IN_PORT=$(grep -c 'type="AUDIO_DEVICE_IN_BLUETOOTH_A2DP"' "$SRC")
HAS_IN_ROUTE=$(grep -c 'sources="BT A2DP In"' "$SRC")
MOVE_TO_PRIMARY=0
[ -f "$WORK/a2dp_in_primary" ] && MOVE_TO_PRIMARY=1

log_i "a2dp module=$HAS_MODULE inPort=$HAS_IN_PORT inRoute=$HAS_IN_ROUTE moveToPrimary=$MOVE_TO_PRIMARY"

if [ "$HAS_MODULE" -ge 1 ] && [ "$HAS_IN_PORT" -ge 1 ] && [ "$HAS_IN_ROUTE" -ge 1 ] && [ "$MOVE_TO_PRIMARY" = "0" ]; then
  log_i "stock policy already routes bt_a2dp_in (BT A2DP In -> a2dp input); verify-only, no mount"
  rm -f "$OUT"
  exit 0
fi

# ---------------------------------------------------------------------------
# Build the patched copy.
# ---------------------------------------------------------------------------
cp -f "$SRC" "$WORK/stock_audio_policy_configuration.xml"
rm -f "$OUT.tmp"

if [ "$MOVE_TO_PRIMARY" = "1" ]; then
  log_i "experiment: moving BT A2DP In into primary module"
  # 1) strip the standalone a2dp module block
  # 2) add the device port into the FIRST (primary) <devicePorts> block
  # 3) append ",BT A2DP In" to the sources of the route whose sink is "primary input"
  awk '
    BEGIN { in_a2dp = 0; primary_ports_done = 0; pending_primary_input = 0 }
    /<module name="a2dp"/ { in_a2dp = 1; next }
    in_a2dp == 1 {
      if ($0 ~ /<\/module>/) { in_a2dp = 0 }
      next
    }
    /<\/devicePorts>/ && primary_ports_done == 0 {
      print "                <devicePort tagName=\"BT A2DP In\" type=\"AUDIO_DEVICE_IN_BLUETOOTH_A2DP\" role=\"source\">"
      print "                    <profile name=\"\" format=\"AUDIO_FORMAT_PCM_16_BIT\""
      print "                             samplingRates=\"44100,48000\""
      print "                             channelMasks=\"AUDIO_CHANNEL_IN_MONO,AUDIO_CHANNEL_IN_STEREO\"/>"
      print "                </devicePort>"
      primary_ports_done = 1
      print; next
    }
    /sink="primary input"/ { pending_primary_input = 1 }
    pending_primary_input == 1 && /sources="/ {
      sub(/"\/>/, ",BT A2DP In\"/>")
      pending_primary_input = 0
    }
    { print }
  ' "$SRC" > "$OUT.tmp"
else
  log_i "a2dp module/port/route missing - re-inserting generic a2dp HAL module"
  # Insert a complete a2dp module right before the USB module comment (stock
  # layout) or, failing that, before the r_submix include.
  awk '
    function emit_module() {
      print "        <!-- A2DP Input Audio HAL (re-inserted by CarRelay) -->"
      print "        <module name=\"a2dp\" halVersion=\"2.0\">"
      print "            <mixPorts>"
      print "                <mixPort name=\"a2dp input\" role=\"sink\">"
      print "                    <profile name=\"\" format=\"AUDIO_FORMAT_PCM_16_BIT\""
      print "                             samplingRates=\"44100,48000\""
      print "                             channelMasks=\"AUDIO_CHANNEL_IN_MONO,AUDIO_CHANNEL_IN_STEREO\"/>"
      print "                </mixPort>"
      print "            </mixPorts>"
      print "            <devicePorts>"
      print "                <devicePort tagName=\"BT A2DP In\" type=\"AUDIO_DEVICE_IN_BLUETOOTH_A2DP\" role=\"source\">"
      print "                    <profile name=\"\" format=\"AUDIO_FORMAT_PCM_16_BIT\""
      print "                             samplingRates=\"44100,48000\""
      print "                             channelMasks=\"AUDIO_CHANNEL_IN_MONO,AUDIO_CHANNEL_IN_STEREO\"/>"
      print "                </devicePort>"
      print "            </devicePorts>"
      print "            <routes>"
      print "                <route type=\"mix\" sink=\"a2dp input\""
      print "                       sources=\"BT A2DP In\"/>"
      print "            </routes>"
      print "        </module>"
      print ""
    }
    BEGIN { done = 0; skip = 0 }
    /<module name="a2dp"/ { skip = 1; next }
    skip == 1 { if ($0 ~ /<\/module>/) { skip = 0 }; next }
    done == 0 && /<!-- Usb Audio HAL -->/ { emit_module(); done = 1 }
    done == 0 && /r_submix_audio_policy_configuration.xml/ { emit_module(); done = 1 }
    { print }
  ' "$SRC" > "$OUT.tmp"
fi

# ---------------------------------------------------------------------------
# Validate before mounting. All checks must pass or we leave stock in place.
# ---------------------------------------------------------------------------
OPEN=$(grep -c '<module name=' "$OUT.tmp")
CLOSE=$(grep -c '</module>' "$OUT.tmp")
PORTS=$(grep -c 'type="AUDIO_DEVICE_IN_BLUETOOTH_A2DP"' "$OUT.tmp")
ROUTES=$(grep -c 'BT A2DP In' "$OUT.tmp")
HEAD_OK=$(head -c 200 "$OUT.tmp" | grep -c '<?xml')
TAIL_OK=$(tail -n 3 "$OUT.tmp" | grep -c '</audioPolicyConfiguration>')

if [ "$OPEN" -ne "$CLOSE" ] || [ "$PORTS" -ne 1 ] || [ "$ROUTES" -lt 2 ] || [ "$HEAD_OK" -ne 1 ] || [ "$TAIL_OK" -ne 1 ]; then
  log_w "validation failed (modules open=$OPEN close=$CLOSE inPorts=$PORTS refs=$ROUTES head=$HEAD_OK tail=$TAIL_OK) - NOT mounting"
  rm -f "$OUT.tmp"
  exit 0
fi

mv -f "$OUT.tmp" "$OUT"
chown root:root "$OUT"
chmod 0644 "$OUT"
chcon "$VENDOR_CTX" "$OUT" || log_w "chcon failed"
log_i "patched policy context: $(ls -Z "$OUT" | awk '{print $1}')"

if mount -o bind "$OUT" "$SRC"; then
  log_i "bind-mounted $OUT over $SRC"
else
  log_w "bind mount failed - stock policy remains active"
fi
exit 0
