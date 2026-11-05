# Produce the modified mixer_paths.xml / audio_platform_info_intcodec.xml (byte-identical
# except for the listed replacements), unified diffs, and verify both still parse.
import sys, os, difflib
import xml.dom.minidom

src = sys.argv[1]
dst = sys.argv[2]
os.makedirs(dst, exist_ok=True)


def patch(name, repls):
    p = os.path.join(src, name)
    data = open(p, 'rb').read().decode('latin1')
    out = data
    for old, new in repls:
        n = out.count(old)
        if n != 1:
            raise SystemExit("%s: anchor found %d times (expected 1):\n%s" % (name, n, old))
        out = out.replace(old, new)
    op = os.path.join(dst, name)
    open(op, 'wb').write(out.encode('latin1'))
    xml.dom.minidom.parse(op)  # raises if not well-formed
    diff = difflib.unified_diff(data.splitlines(True), out.splitlines(True),
                                fromfile='a/vendor/etc/' + name, tofile='b/vendor/etc/' + name, n=3)
    open(os.path.join(dst, name.replace('.xml', '.diff')), 'wb').write(''.join(diff).encode('latin1'))
    print("%s: %d -> %d bytes, well-formed" % (name, len(data), len(out)))


MP_DEFAULTS_OLD = """    <!-- HFP start -->
    <ctl name="HFP_SLIM7_UL_HL Switch" value="0" />
    <ctl name="RX_CDC_DMA_RX_1 Port Mixer SLIM_7_TX" value="0" />
    <ctl name="RX_CDC_DMA_RX_0 Port Mixer SLIM_7_TX" value="0" />
    <!-- HFP end -->
"""
MP_DEFAULTS_NEW = """    <!-- HFP start -->
    <ctl name="HFP_SLIM7_UL_HL Switch" value="0" />
    <ctl name="SCO_SLIM7_DL_HL Switch" value="0" />
    <ctl name="RX_CDC_DMA_RX_1 Port Mixer SLIM_7_TX" value="0" />
    <ctl name="RX_CDC_DMA_RX_0 Port Mixer SLIM_7_TX" value="0" />
    <ctl name="MultiMedia6 Mixer TX_CDC_DMA_TX_3" value="0" />
    <ctl name="MultiMedia6 Mixer TX_CDC_DMA_TX_4" value="0" />
    <!-- HFP end -->
"""

MP_HFP_OLD = """    <path name="hfp-sco">
    </path>

    <path name="hfp-sco headphones">
    </path>

   <path name="hfp-sco-wb">
        <path name="hfp-sco" />
   </path>

    <path name="hfp-sco-wb headphones">
        <path name="hfp-sco headphones" />
    </path>
"""
MP_HFP_NEW = """    <!-- HFP client (tablet acts as the hands-free unit of a phone).
         libhfp opens: pcm 33 (MultiMedia6, ASM loopback) as "asm" rx+tx and
         pcm 35 (SLIMBUS_7 Hostless) as rx+tx; see audio_platform_info_intcodec.xml.
         Uplink   (tablet mic -> phone): TX_CDC_DMA_TX_3 -> MultiMedia6 -> SLIMBUS_7_RX,
                  entirely in the DSP (ASM loopback does the 48k -> 8k/16k SRC).
         Downlink (phone -> tablet): this kernel has no "<RX> Port Mixer SLIM_7_TX"
                  for PRI_TDM_RX_0 or RX_CDC_DMA_RX_x, and MultiMedia6 has no SLIM_7_TX
                  input, so SLIM_7_TX is captured on MultiMedia10 (pcm 12) by a small
                  user-space bridge and played back through MultiMedia10 (pcm 12) into
                  the speaker (PRI_TDM_RX_0) or headphone (RX_CDC_DMA_RX_0) backend.
         The *_HL switches give the hostless FE (pcm 35) a DAPM route so that
                  SLIMBUS_7_RX / SLIMBUS_7_TX are powered for the whole call. -->
    <path name="hfp-sco">
        <ctl name="SCO_SLIM7_DL_HL Switch" value="1" />
        <ctl name="HFP_SLIM7_UL_HL Switch" value="1" />
        <ctl name="MultiMedia6 Mixer TX_CDC_DMA_TX_3" value="1" />
        <ctl name="SLIMBUS_7_RX Audio Mixer MultiMedia6" value="1" />
        <ctl name="MultiMedia10 Mixer SLIM_7_TX" value="1" />
        <ctl name="PRI_TDM_RX_0 Audio Mixer MultiMedia10" value="1" />
        <!-- optional echo reference for the uplink (needs an AEC topology in ACDB):
        <ctl name="AUDIO_REF_EC_UL6 MUX" value="PRI_TDM_RX_0" />
        <ctl name="EC Reference Channels" value="One"/> -->
    </path>

    <path name="hfp-sco headphones">
        <ctl name="SCO_SLIM7_DL_HL Switch" value="1" />
        <ctl name="HFP_SLIM7_UL_HL Switch" value="1" />
        <ctl name="MultiMedia6 Mixer TX_CDC_DMA_TX_3" value="1" />
        <ctl name="SLIMBUS_7_RX Audio Mixer MultiMedia6" value="1" />
        <ctl name="MultiMedia10 Mixer SLIM_7_TX" value="1" />
        <ctl name="RX_CDC_DMA_RX_0 Audio Mixer MultiMedia10" value="1" />
    </path>

    <path name="hfp-sco headset">
        <ctl name="SCO_SLIM7_DL_HL Switch" value="1" />
        <ctl name="HFP_SLIM7_UL_HL Switch" value="1" />
        <ctl name="MultiMedia6 Mixer TX_CDC_DMA_TX_4" value="1" />
        <ctl name="SLIMBUS_7_RX Audio Mixer MultiMedia6" value="1" />
        <ctl name="MultiMedia10 Mixer SLIM_7_TX" value="1" />
        <ctl name="RX_CDC_DMA_RX_0 Audio Mixer MultiMedia10" value="1" />
    </path>

    <path name="hfp-sco-wb">
        <ctl name="BT SampleRate" value="KHZ_16" />
        <path name="hfp-sco" />
    </path>

    <path name="hfp-sco-wb headphones">
        <ctl name="BT SampleRate" value="KHZ_16" />
        <path name="hfp-sco headphones" />
    </path>

    <path name="hfp-sco-wb headset">
        <ctl name="BT SampleRate" value="KHZ_16" />
        <path name="hfp-sco headset" />
    </path>
"""

MP_DEV_OLD = """    <path name="voice-speaker-mic">
        <path name="speaker-mic" />
    </path>
"""
MP_DEV_NEW = """    <path name="voice-speaker-mic">
        <path name="speaker-mic" />
    </path>

    <!-- HFP client: the HAL selects SND_DEVICE_OUT_VOICE_SPEAKER_HFP /
         SND_DEVICE_IN_VOICE_SPEAKER_MIC_HFP while hfp_enable=true and the
         output device is the speaker. Same hardware as the voice-call paths. -->
    <path name="voice-speaker-hfp">
        <path name="speaker" />
    </path>

    <path name="voice-speaker-mic-hfp">
        <path name="speaker-mic" />
    </path>
"""

PI_PCM_OLD = """        <usecase name="USECASE_AUDIO_HFP_SCO" type="in" id="12" />
        <usecase name="USECASE_AUDIO_HFP_SCO_WB" type="in" id="12" />
"""
PI_PCM_NEW = """        <usecase name="USECASE_AUDIO_HFP_SCO" type="out" id="35" />
        <usecase name="USECASE_AUDIO_HFP_SCO" type="in" id="35" />
        <usecase name="USECASE_AUDIO_HFP_SCO_WB" type="out" id="35" />
        <usecase name="USECASE_AUDIO_HFP_SCO_WB" type="in" id="35" />
"""
PI_PARAM_OLD = """        <param key="hfp_pcm_dev_id" value="39"/>
"""
PI_PARAM_NEW = """        <param key="hfp_pcm_dev_id" value="33"/>
"""

patch('mixer_paths.xml', [(MP_DEFAULTS_OLD, MP_DEFAULTS_NEW), (MP_HFP_OLD, MP_HFP_NEW), (MP_DEV_OLD, MP_DEV_NEW)])
patch('audio_platform_info_intcodec.xml', [(PI_PCM_OLD, PI_PCM_NEW), (PI_PARAM_OLD, PI_PARAM_NEW)])
