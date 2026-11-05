#!/usr/bin/env python3
# Stage A: dual-capable A2DP registration for libbluetooth_qti.so (TB-J606F S320311).
# From ORIGINAL. v1 (sink worker) + v2 (remote-start suppress, rx_flush clear) +
# v5 dual-capable registration:
#   - bta_av_api_register: make the SOURCE (0x110a) registration create SEPs for
#     codec indices 0..9 (was 0..6) -> two in-place words.
#   - CAVE5B: at the per-codec loop top, set is_sink_flag([sp,#0xfc]) and the AVDT
#     data cback cs.p_data_cback([sp,#0xe8]) from (codec_index>=7): indices 7..9 become
#     SNK SEPs (tsep=1, p_app_sink_data_cback=&bte_av_sink_media_callback via the
#     callback the source reg already passes, p_data_cback=&bta_av_sink_data_cback);
#     0..6 stay SRC. Generalizes cleanly for the 0x110b path too.
# NOTE: v3/CAVE4 (separate sink BTA_AvRegister) is REMOVED; dual-capable scbs replace it.
# Verifies every original byte; refuses on mismatch; verifies result by disasm.
import sys,struct,hashlib
from capstone import Cs,CS_ARCH_ARM64,CS_MODE_ARM
SRC='libbluetooth_qti.so'; OUT='fable/libbluetooth_qti.patched5a.so'

INIT_SINK_CALL=0x1c11a8; ORIG_CALL =0x97ffbe7c
OPENED_HOOK   =0x1b6be4; ORIG_OPENED=0x39000268
UPDEC_HOOK    =0x1a4f70; ORIG_UPDEC =0xd100c3ff
REGLOOP_HOOK  =0x128e68; ORIG_REGLOOP=0x396c0288   # ldrb w8,[x20,#0xb00]
SRC_SETUP_A   =0x128da4; ORIG_SA=0x3903f3ff        # strb wzr,[sp,#0xfc]
SRC_SETUP_B   =0x128da8; ORIG_SB=0x1400000d        # b 0x128ddc
BTIF_AV_INIT=0x1b0b98; SINK_STARTUP=0x1a4870; IS_SINK_ENABLED=0x1be6d4
OPENED_CONT=0x1b6be8; OPENED_SKIP=0x1b6bf8; UPDEC_CONT=0x1a4f74
REGLOOP_CONT=0x128e6c; REG_SRC_END=0x128de0
SINK_DATA_CBACK_PAGE=0x3cc000  # +0x864 = &bta_av_sink_data_cback (0x3cc864)

CAVE1=0x0f5118; CAVE2=0x0f5138; CAVE3=0x0f5168; CAVE5B=0x0f5178

def bl(f,t): o=t-f; assert -(1<<27)<=o<(1<<27); return 0x94000000|((o>>2)&0x03ffffff)
def b (f,t): o=t-f; assert -(1<<27)<=o<(1<<27); return 0x14000000|((o>>2)&0x03ffffff)
def cbnz_w(r,f,t): o=t-f; assert -(1<<20)<=o<(1<<20); return 0x35000000|(((o>>2)&0x7ffff)<<5)|r
def bcond(cond,f,t): o=t-f; assert -(1<<20)<=o<(1<<20); return 0x54000000|(((o>>2)&0x7ffff)<<5)|cond
def adrp(f,page,reg): off=(page&~0xfff)-(f&~0xfff); imm=off>>12; return 0x90000000|((imm&3)<<29)|(((imm>>2)&0x7ffff)<<5)|reg

def main():
    bb=bytearray(open(SRC,'rb').read())
    rd=lambda a: struct.unpack_from('<I',bb,a)[0]; wr=lambda a,w: struct.pack_into('<I',bb,a,w)
    for a,exp,nm in [(INIT_SINK_CALL,ORIG_CALL,'init_sink'),(OPENED_HOOK,ORIG_OPENED,'opened'),
                     (UPDEC_HOOK,ORIG_UPDEC,'updec'),(REGLOOP_HOOK,ORIG_REGLOOP,'regloop'),
                     (SRC_SETUP_A,ORIG_SA,'srcA'),(SRC_SETUP_B,ORIG_SB,'srcB')]:
        if rd(a)!=exp: sys.exit(f"ABORT {nm}@{a:#x}: {rd(a):#010x}!={exp:#010x}")
    for i in range(CAVE1,CAVE5B+0x40):
        if bb[i]!=0: sys.exit(f"ABORT cave byte {i:#x} not zero")
    c1=[0xa9be7bfd,0x910003fd,bl(CAVE1+0x08,BTIF_AV_INIT),0xb90013e0,
        bl(CAVE1+0x10,SINK_STARTUP),0xb94013e0,0xa8c27bfd,0xd65f03c0]
    c2=[0xa9be53f3,0xa90163f7,bl(CAVE2+0x08,IS_SINK_ENABLED),0x2a0003e9,
        0xa94163f7,0xa8c253f3,cbnz_w(9,CAVE2+0x18,CAVE2+0x2c),0x52800028,
        0x39000268,0x52801809,b(CAVE2+0x28,OPENED_CONT),b(CAVE2+0x2c,OPENED_SKIP)]
    c3=[adrp(CAVE3,0x3f3000,16),0x392fa21f,ORIG_UPDEC,b(CAVE3+0x0c,UPDEC_CONT)]
    # CAVE5B: per-iteration sink-flag + AVDT data cback from (x25>=7)
    L=CAVE5B
    c5=[0xf1001f3f,                 # +00 cmp x25,#7
        bcond(0x3,L+0x04,L+0x20),   # +04 b.lo .src (LO=3)
        0x52800028,                 # +08 mov w8,#1
        0x3903f3e8,                 # +0c strb w8,[sp,#0xfc]
        adrp(L+0x10,SINK_DATA_CBACK_PAGE,8), # +10 adrp x8,0x3cc000
        0x91219108,                 # +14 add x8,x8,#0x864
        0xf90077e8,                 # +18 str x8,[sp,#0xe8]
        b(L+0x1c,L+0x28),           # +1c b .cont
        0x3903f3ff,                 # +20 .src strb wzr,[sp,#0xfc]
        0xf90077ff,                 # +24 str xzr,[sp,#0xe8]
        ORIG_REGLOOP,               # +28 .cont ldrb w8,[x20,#0xb00]
        b(L+0x2c,REGLOOP_CONT)]     # +2c b 0x128e6c
    for cave,base in [(c1,CAVE1),(c2,CAVE2),(c3,CAVE3),(c5,CAVE5B)]:
        for i,w in enumerate(cave): wr(base+4*i,w)
    wr(INIT_SINK_CALL,bl(INIT_SINK_CALL,CAVE1))
    wr(OPENED_HOOK,b(OPENED_HOOK,CAVE2))
    wr(UPDEC_HOOK,b(UPDEC_HOOK,CAVE3))
    wr(REGLOOP_HOOK,b(REGLOOP_HOOK,CAVE5B))
    wr(SRC_SETUP_A,0x52800155)              # mov w21,#0xa (source loop end=10)
    wr(SRC_SETUP_B,b(SRC_SETUP_B,REG_SRC_END))  # b 0x128de0 (skip w21=7)
    open(OUT,'wb').write(bb)
    md=Cs(CS_ARCH_ARM64,CS_MODE_ARM)
    def show(a,n,l):
        print(f"-- {l} @ {a:#x} --")
        for ins in md.disasm(bytes(bb[a:a+n*4]),a):
            print(f"  {ins.address:#08x}: {ins.bytes.hex():<10} {ins.mnemonic:7s}{ins.op_str}")
    for a,n,l in [(INIT_SINK_CALL,1,'init_sink hook'),(CAVE1,8,'CAVE1'),(OPENED_HOOK,1,'opened hook'),
                  (CAVE2,12,'CAVE2'),(UPDEC_HOOK,1,'updec hook'),(CAVE3,4,'CAVE3'),
                  (SRC_SETUP_A,1,'srcA inplace'),(SRC_SETUP_B,1,'srcB inplace'),
                  (REGLOOP_HOOK,1,'regloop hook'),(CAVE5B,12,'CAVE5B')]:
        show(a,n,l)
    print("WROTE",OUT,len(bb),"bytes; sha1",hashlib.sha1(bytes(bb)).hexdigest())
main()
