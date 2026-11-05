"""Summarise AVCTP (AVRCP control) and browse traffic from a btsnoop file. Prints PDU ids only, no payload strings."""
import struct
import sys
import datetime

PDU = {0x10: 'GetCapabilities', 0x20: 'GetElementAttributes', 0x30: 'GetPlayStatus', 0x31: 'RegisterNotification',
       0x50: 'SetAbsoluteVolume', 0x60: 'SetAddressedPlayer', 0x70: 'SetBrowsedPlayer', 0x71: 'GetFolderItems',
       0x40: 'RequestContinuation', 0x41: 'AbortContinuation', 0x17: 'InformDisplayableCharset',
       0x11: 'ListPlayerAppSettingAttrs', 0x13: 'GetCurrentPlayerAppSettingValue'}
CT = {0: 'CMD_CONTROL', 1: 'CMD_STATUS', 2: 'CMD_SPEC_INQ', 3: 'CMD_NOTIFY', 4: 'CMD_GEN_INQ', 8: 'RSP_NOT_IMPL', 9: 'RSP_ACCEPT',
      10: 'RSP_REJECT', 11: 'RSP_IN_TRANSITION', 12: 'RSP_STABLE', 13: 'RSP_CHANGED', 15: 'RSP_INTERIM'}

data = open(sys.argv[1], 'rb').read()
assert data[:8] == b'btsnoop\0', 'not a btsnoop file'
pos = 16
chan = {}      # (handle, dir-local cid) -> psm
pending = {}   # scid awaiting connect response -> psm
partial = {}
n = counts = 0
t0 = None
while pos + 24 <= len(data):
    olen, ilen, flags, drops, ts = struct.unpack('>IIIIq', data[pos:pos + 24])
    pkt = data[pos + 24:pos + 24 + ilen]
    pos += 24 + ilen
    n += 1
    if not pkt or pkt[0] != 2 or len(pkt) < 5:
        continue
    h = struct.unpack('<H', pkt[1:3])[0]
    handle, pb = h & 0xfff, (h >> 12) & 3
    body = pkt[5:]
    rx = bool(flags & 1)
    if pb == 1:
        continue
    if len(body) < 4:
        continue
    l2len, cid = struct.unpack('<HH', body[:4])
    pl = body[4:]
    if cid == 1 and len(pl) >= 4:   # signalling
        code = pl[0]
        if code == 2 and len(pl) >= 8:   # connection request
            psm, scid = struct.unpack('<HH', pl[4:8])
            pending[(handle, scid, rx)] = psm
        elif code == 3 and len(pl) >= 12:   # connection response
            dcid, scid, res = struct.unpack('<HHH', pl[4:10])
            psm = pending.get((handle, scid, not rx))
            if psm is not None:
                if res == 0:
                    chan[(handle, dcid, rx)] = psm       # peer's cid for what the responder receives
                    chan[(handle, scid, not rx)] = psm
                print('%8s L2CAP connect psm=%#x result=%d' % ('', psm, res))
        continue
    # On receive, the CID is ours (scid or dcid); look up either direction key.
    psm = chan.get((handle, cid, rx)) or chan.get((handle, cid, not rx))
    if psm not in (0x17, 0x1b):
        continue
    t = datetime.datetime(1, 1, 1) + datetime.timedelta(microseconds=ts - 0x00E03AB44A676000 + 0xE03AB44A676000 - 0) if False else None
    sec = ts / 1e6
    if t0 is None:
        t0 = sec
    who = 'iPhone->tablet' if rx else 'tablet->iPhone'
    if psm == 0x1b:
        print('%8.3f %s BROWSE len=%d' % (sec - t0, who, len(pl)))
        continue
    if len(pl) < 6:
        continue
    avctp = pl[0]
    cr = 'rsp' if avctp & 2 else 'cmd'
    pid = (pl[1] << 8) | pl[2]
    ctype = pl[3] & 0xf
    op = pl[5]
    label = avctp >> 4
    desc = 'opcode=%#x' % op
    if op == 0x7c:
        desc = 'PASSTHROUGH key=%#x' % (pl[6] & 0x7f) if len(pl) > 6 else 'PASSTHROUGH'
    elif op == 0 and len(pl) > 9:
        p = pl[9]
        desc = 'VENDOR %s' % PDU.get(p, hex(p))
        if p == 0x10 and len(pl) > 13:
            desc += ' capid=%d' % pl[13]
        if p == 0x31 and len(pl) > 13:
            desc += ' event=%#x' % pl[13]
    print('%8.3f %s lbl=%-2d %s %-12s %s' % (sec - t0, who, label, cr, CT.get(ctype, ctype), desc))
    counts += 1
print('packets', n, 'avctp frames', counts)
