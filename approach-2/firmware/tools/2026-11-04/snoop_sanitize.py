"""Sanitise a btsnoop file for sharing; the output is still a valid btsnoop file for Wireshark.

- Link keys zeroed: HCI Link_Key_Request_Reply (0x040B) and Link_Key_Notification event (0x18).
- Names and neighbours removed: Remote_Name_Request_Complete name zeroed; inquiry results and LE advertising reports dropped.
- ACL payload kept only for L2CAP signalling, SDP, AVCTP control and browsing, and AVDTP (AVDTP media cut to 32 bytes).
  Everything else, including RFCOMM (hands-free AT commands) and OBEX/PBAP, is cut to its L2CAP header.
- Known Bluetooth addresses replaced in every packet.
"""
import struct
import sys

src, dst = sys.argv[1], sys.argv[2]
ADDR = {  # as written in the clear, replaced in the little-endian form used on the wire
    'AA:00:00:00:00:01': 'BB:00:00:00:00:16',
    'AA:00:00:00:00:02': 'BB:00:00:00:00:17',
}
for a in sys.argv[3:]:
    real, ph = a.split('=')
    ADDR[real.upper()] = ph


def le(m):
    return bytes(int(x, 16) for x in reversed(m.split(':')))


REPL = [(le(k), le(v)) for k, v in ADDR.items()]
KEEP_PSM = {0x0001, 0x0017, 0x001B, 0x0019}
d = open(src, 'rb').read()
assert d[:8] == b'btsnoop\0'
out = bytearray(d[:16])
pos = 16
chan = {}
pend = {}
stats = dict(total=0, dropped=0, keys=0, names=0, cut=0)
while pos + 24 <= len(d):
    olen, ilen, flags, drops, ts = struct.unpack('>IIIIq', d[pos:pos + 24])
    pkt = bytearray(d[pos + 24:pos + 24 + ilen])
    pos += 24 + ilen
    stats['total'] += 1
    keep = True
    rx = bool(flags & 1)
    if pkt and pkt[0] == 1 and len(pkt) >= 4:     # HCI command
        op = pkt[1] | pkt[2] << 8
        if op == 0x040B and len(pkt) >= 26:
            pkt[10:26] = bytes(16)
            stats['keys'] += 1
    elif pkt and pkt[0] == 4 and len(pkt) >= 3:   # HCI event
        ev = pkt[1]
        if ev == 0x18 and len(pkt) >= 25:
            pkt[9:25] = bytes(16)
            stats['keys'] += 1
        elif ev == 0x07 and len(pkt) >= 10:
            pkt[10:] = bytes(len(pkt) - 10)
            stats['names'] += 1
        elif ev in (0x02, 0x22, 0x2F):            # inquiry results: other people's devices
            keep = False
        elif ev == 0x3E and len(pkt) >= 4 and pkt[3] in (0x02, 0x0B, 0x0D):   # LE advertising reports
            keep = False
    elif pkt and pkt[0] == 2 and len(pkt) >= 9:   # ACL
        h = struct.unpack('<H', pkt[1:3])[0]
        hd, pb = h & 0xfff, (h >> 12) & 3
        ln, cid = struct.unpack('<HH', pkt[5:9]) if pb != 1 else (0, None)
        if pb == 1:   # continuation fragment: payload is unknown, cut it
            del pkt[5:]
            stats['cut'] += 1
        elif cid == 1 and len(pkt) >= 13:
            code = pkt[9]
            if code == 2 and len(pkt) >= 17:
                psm, scid = struct.unpack('<HH', pkt[13:17])
                pend[(hd, scid, rx)] = psm
            elif code == 3 and len(pkt) >= 21:
                dcid, scid, res = struct.unpack('<HHH', pkt[13:19])
                psm = pend.get((hd, scid, not rx))
                if psm is not None and res == 0:
                    chan[(hd, dcid, rx)] = psm
                    chan[(hd, scid, not rx)] = psm
        elif cid is not None and cid != 1:
            psm = chan.get((hd, cid, rx)) or chan.get((hd, cid, not rx))
            if psm not in KEEP_PSM:
                del pkt[9:]
                stats['cut'] += 1
            elif psm == 0x0019 and len(pkt) > 9 + 32 and ln > 64:   # AVDTP media, not signalling
                del pkt[9 + 32:]
                stats['cut'] += 1
    if not keep:
        stats['dropped'] += 1
        continue
    pkt = bytes(pkt)
    for a, b in REPL:
        pkt = pkt.replace(a, b)
    out += struct.pack('>IIIIq', olen, len(pkt), flags, drops, ts) + pkt
open(dst, 'wb').write(out)
print(src.split('\\')[-1].split('/')[-1], stats, 'bytes', len(d), '->', len(out))
