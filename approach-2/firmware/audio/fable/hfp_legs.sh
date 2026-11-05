#!/system/bin/sh
# HFP-client audio bring-up helper for the Lenovo Tab P11 (bengal-idp-snd-card).
# Run as root on the tablet:  sh /data/local/tmp/fable/hfp_legs.sh <command>
# Nothing here touches /vendor; every mixer change has a matching "-off" command.
#
# PCM map (card 0):  12 = MultiMedia10 (bridge capture + playback)
#                    33 = MultiMedia6  (ASM loopback, hfp_pcm_dev_id, uplink mic->BT)
#                    35 = SLIMBUS_7 Hostless (libhfp rx/tx, powers SLIMBUS_7_RX/TX)
M="tinymix -D 0"
T=/data/local/tmp/fable
RATE=${RATE:-16000}     # 8000 for a narrow-band SCO link
CH=${CH:-2}             # bridge channel count (2 = same mapping as music; try 1 if odd)

speaker_path() {        # == mixer_paths "speaker" path
  $M "PRI_TDM_RX_0 Channels" Four
  $M "PRI_TDM_RX_0 Format" S32_LE
  $M "PRI_TDM_RX_0 SampleRate" KHZ_48
  for i in 0 1 2 3; do $M "aw_dev_${i}_switch" $1; done
}
headphones_path() {     # == mixer_paths "headphones" path ($1 = 1/0, $2 = enum values)
  $M "RX_MACRO RX0 MUX" $2
  $M "RX_MACRO RX1 MUX" $2
  $M "RX_CDC_DMA_RX_0 Channels" $3
  $M "RX INT0_1 MIX1 INP0" $4
  $M "RX INT1_1 MIX1 INP0" $5
  $M "RX INT0 DEM MUX" $6
  $M "RX INT1 DEM MUX" $6
  for c in "RX_COMP1 Switch" "RX_COMP2 Switch" "HPHL_COMP Switch" "HPHR_COMP Switch" "HPHL_RDAC Switch" "HPHR_RDAC Switch"; do $M "$c" $1; done
}
amic3_path() {          # == mixer_paths "amic3" (speaker-mic). $1 = 1/0
  if [ "$1" = 1 ]; then
    $M "TX DEC2 MUX" SWR_MIC
    $M "TX SMIC MUX2" SWR_MIC4
    $M "TX_CDC_DMA_TX_3 Channels" One
    $M "TX_AIF1_CAP Mixer DEC2" 1
    $M "ADC2_MIXER Switch" 1
    $M "ADC2 MUX" INP3
  else
    $M "TX_AIF1_CAP Mixer DEC2" 0
    $M "ADC2_MIXER Switch" 0
    $M "TX SMIC MUX2" ZERO
    $M "TX DEC2 MUX" MSM_DMIC
  fi
}

case "$1" in
info)
  for d in 12 23 33 35; do echo "== pcm $d =="; tinypcminfo -D 0 -d $d; done
  echo "== AFE_LOOPBACK_TX Port options =="; $M "AFE_LOOPBACK_TX Port"
  echo "== tinyplay / tinycap usage (check the stdin/stdout spelling) =="; tinyplay 2>&1 | head -20; tinycap 2>&1 | head -5
  ;;
state)
  for c in "HFP_SLIM7_UL_HL Switch" "SCO_SLIM7_DL_HL Switch" "MultiMedia6 Mixer TX_CDC_DMA_TX_3" "SLIMBUS_7_RX Audio Mixer MultiMedia6" \
           "MultiMedia10 Mixer SLIM_7_TX" "PRI_TDM_RX_0 Audio Mixer MultiMedia10" "RX_CDC_DMA_RX_0 Audio Mixer MultiMedia10" \
           "BT SampleRate" "BT SampleRate RX" "BT SampleRate TX" "PRI_TDM_RX_0 Channels" "PRI_TDM_RX_0 Format" "aw_dev_0_switch" \
           "TX_AIF1_CAP Mixer DEC2" "ADC2 MUX" "Playback 33 Volume" "Playback 12 Volume" "Internal HFP RX Volume" "HFP TX Mute"; do
    printf '%-44s ' "$c"; $M "$c" 2>&1 | tail -1
  done
  for p in pcm12c pcm12p pcm33p pcm33c pcm35p pcm35c; do
    printf '%-8s ' $p; head -1 /proc/asound/card0/$p/sub0/status 2>/dev/null || echo closed
  done
  ;;
spk-on)   speaker_path Enable;  $M "PRI_TDM_RX_0 Audio Mixer MultiMedia10" 1 ;;
spk-off)  $M "PRI_TDM_RX_0 Audio Mixer MultiMedia10" 0; speaker_path Disable ;;
hp-on)    headphones_path 1 AIF1_PB Two RX0 RX1 CLSH_DSM_OUT; $M "RX_CDC_DMA_RX_0 Audio Mixer MultiMedia10" 1 ;;
hp-off)   $M "RX_CDC_DMA_RX_0 Audio Mixer MultiMedia10" 0; headphones_path 0 ZERO One ZERO ZERO NORMAL_DSM_OUT ;;
play)     tinyplay $T/tone_16k_stereo.wav -D 0 -d 12 -p 240 -n 4 ;;
mic-on)   amic3_path 1; $M "MultiMedia10 Mixer TX_CDC_DMA_TX_3" 1 ;;
mic-off)  $M "MultiMedia10 Mixer TX_CDC_DMA_TX_3" 0; amic3_path 0 ;;
rec)      tinycap $T/mic.wav -D 0 -d 12 -c 1 -r $RATE -b 16 -p 240 -n 4 -t 5; ls -l $T/mic.wav ;;
bridge-start)
  # Downlink bridge: SLIM_7_TX -> MultiMedia10 capture -> pipe -> MultiMedia10 playback -> speaker/headphones.
  # The mixer for both halves is set by the HAL's "hfp-sco*" path while hfp_enable=true (or by spk-on/mic-on for dry runs).
  # If "tinyplay -" is rejected, use "--" (see the usage text printed by the info command).
  ( tinycap -- -D 0 -d 12 -c $CH -r $RATE -b 16 -p 240 -n 4 | tinyplay - -D 0 -d 12 -i raw -c $CH -r $RATE -b 16 -p 240 -n 4 ) &
  echo "bridge started (rate=$RATE ch=$CH)"
  ;;
bridge-stop)
  pkill -f "tinycap -- -D 0 -d 12"; pkill -f "tinyplay - -D 0 -d 12"; echo "bridge stopped"
  ;;
loop-dry)
  # Dry run without a phone: mic -> MultiMedia10 capture -> bridge -> MultiMedia10 playback -> speaker (expect to hear yourself, ~100 ms late)
  amic3_path 1; $M "MultiMedia10 Mixer TX_CDC_DMA_TX_3" 1; speaker_path Enable; $M "PRI_TDM_RX_0 Audio Mixer MultiMedia10" 1
  sh $0 bridge-start
  ;;
loop-dry-off)
  sh $0 bridge-stop; $M "PRI_TDM_RX_0 Audio Mixer MultiMedia10" 0; speaker_path Disable; $M "MultiMedia10 Mixer TX_CDC_DMA_TX_3" 0; amic3_path 0
  ;;
mount)
  mount -o bind $T/mixer_paths.xml /vendor/etc/mixer_paths.xml
  mount -o bind $T/audio_platform_info_intcodec.xml /vendor/etc/audio_platform_info_intcodec.xml
  mount | grep -E 'mixer_paths|audio_platform_info_intcodec'
  ;;
umount)
  umount /vendor/etc/mixer_paths.xml; umount /vendor/etc/audio_platform_info_intcodec.xml
  mount | grep -E 'mixer_paths|audio_platform_info_intcodec' || echo "originals back"
  ;;
*)
  echo "usage: $0 info|state|spk-on|spk-off|hp-on|hp-off|play|mic-on|mic-off|rec|bridge-start|bridge-stop|loop-dry|loop-dry-off|mount|umount"
  ;;
esac
