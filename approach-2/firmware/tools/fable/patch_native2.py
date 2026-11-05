#!/usr/bin/env python3
# Combined native patch v2 for libbluetooth_qti.so (Lenovo TB-J606F, S320311).
# From the ORIGINAL .so, applies:
#   v1  (init_sink -> CAVE1: also run btif_a2dp_sink_startup; sink worker/queue)
#   v2a (opened_handler START -> CAVE2: when sink enabled, skip remote-start flag
#        + honor_remote_start, so the 3s source remote-start suspend never fires)
#   v2b (btif_a2dp_sink_update_decoder -> CAVE3: clear rx_flush so received audio
#        is no longer dropped by enqueue_buf/handle_inc_media)
# Caves live in the 0xf5118..0xf6000 UDF page padding. Verifies every original
# byte; refuses on any mismatch. Verifies result by disassembly.
import sys,struct,hashlib
from capstone import Cs,CS_ARCH_ARM64,CS_MODE_ARM

SRC='libbluetooth_qti.so'; OUT='fable/libbluetooth_qti.patched2.so'

# --- addresses (file off == vaddr in .text) ---
INIT_SINK_CALL = 0x1c11a8; ORIG_CALL = 0x97ffbe7c          # bl btif_av_init
OPENED_HOOK    = 0x1b6be4; ORIG_OPENED = 0x39000268        # strb w8,[x19]
UPDEC_HOOK     = 0x1a4f70; ORIG_UPDEC  = 0xd100c3ff        # sub sp,sp,#0x30
BTIF_AV_INIT   = 0x1b0b98
SINK_STARTUP   = 0x1a4870
IS_SINK_ENABLED= 0x1be6d4
OPENED_CONT    = 0x1b6be8     # original madd (remote-start setup) - not-sink path
OPENED_SKIP    = 0x1b6bf8     # post-honor continuation - sink path
UPDEC_CONT     = 0x1a4f74     # 2nd insn of update_decoder

CAVE1=0x0f5118; CAVE2=0x0f5138; CAVE3=0x0f5168

def bl(frm,to): off=to-frm; assert -(1<<27)<=off<(1<<27); return 0x94000000|((off>>2)&0x03ffffff)
def b (frm,to): off=to-frm; assert -(1<<27)<=off<(1<<27); return 0x14000000|((off>>2)&0x03ffffff)
def cbnz_w(reg,frm,to): off=to-frm; assert -(1<<20)<=off<(1<<20); return 0x35000000|(((off>>2)&0x7ffff)<<5)|reg
def adrp(frm,page_target,reg): off=(page_target & ~0xfff)-(frm & ~0xfff); imm=off>>12; return 0x90000000|((imm&3)<<29)|(((imm>>2)&0x7ffff)<<5)|reg

def main():
    b_=bytearray(open(SRC,'rb').read())
    def rd(a): return struct.unpack_from('<I',b_,a)[0]
    def wr(a,w): struct.pack_into('<I',b_,a,w)
    # verify originals
    checks=[(INIT_SINK_CALL,ORIG_CALL,'init_sink bl'),(OPENED_HOOK,ORIG_OPENED,'opened strb'),
            (UPDEC_HOOK,ORIG_UPDEC,'update_decoder sub')]
    for a,exp,name in checks:
        if rd(a)!=exp: sys.exit(f"ABORT {name} @ {a:#x}: {rd(a):#010x} != {exp:#010x}")
    # cave region must be all-zero from CAVE1..CAVE3+0x10
    for i in range(CAVE1, CAVE3+0x10):
        if b_[i]!=0: sys.exit(f"ABORT: cave byte {i:#x} not zero")
    # CAVE1 (v1)
    c1=[0xa9be7bfd,0x910003fd,bl(CAVE1+0x08,BTIF_AV_INIT),0xb90013e0,
        bl(CAVE1+0x10,SINK_STARTUP),0xb94013e0,0xa8c27bfd,0xd65f03c0]
    for i,w in enumerate(c1): wr(CAVE1+4*i,w)
    wr(INIT_SINK_CALL, bl(INIT_SINK_CALL,CAVE1))
    # CAVE2 (v2a): sink-enabled gate around remote-start flag + honor
    c2=[0xa9be53f3,                 # stp x19,x20,[sp,#-0x20]!
        0xa90163f7,                 # stp x23,x24,[sp,#0x10]
        bl(CAVE2+0x08,IS_SINK_ENABLED),
        0x2a0003e9,                 # mov w9,w0
        0xa94163f7,                 # ldp x23,x24,[sp,#0x10]
        0xa8c253f3,                 # ldp x19,x20,[sp],#0x20
        cbnz_w(9,CAVE2+0x18,CAVE2+0x2c),  # cbnz w9,skip
        0x52800028,                 # mov w8,#1
        0x39000268,                 # strb w8,[x19]
        0x52801809,                 # mov w9,#0xc0
        b(CAVE2+0x28,OPENED_CONT),  # b 0x1b6be8  (not-sink: run original)
        b(CAVE2+0x2c,OPENED_SKIP)]  # b 0x1b6bf8  (sink: skip honor)
    for i,w in enumerate(c2): wr(CAVE2+4*i,w)
    wr(OPENED_HOOK, b(OPENED_HOOK,CAVE2))
    # CAVE3 (v2b): clear rx_flush (0x3f3be8) at update_decoder entry
    c3=[adrp(CAVE3+0x00,0x3f3000,16),  # adrp x16,0x3f3000
        0x392fa21f,                    # strb wzr,[x16,#0xbe8]
        ORIG_UPDEC,                    # sub sp,sp,#0x30 (displaced)
        b(CAVE3+0x0c,UPDEC_CONT)]      # b 0x1a4f74
    for i,w in enumerate(c3): wr(CAVE3+4*i,w)
    wr(UPDEC_HOOK, b(UPDEC_HOOK,CAVE3))
    open(OUT,'wb').write(b_)
    # verify
    md=Cs(CS_ARCH_ARM64,CS_MODE_ARM)
    def show(a,n,lbl):
        print(f"-- {lbl} @ {a:#x} --")
        for ins in md.disasm(bytes(b_[a:a+n*4]),a):
            print(f"  {ins.address:#08x}: {ins.bytes.hex():<10} {ins.mnemonic} {ins.op_str}")
    show(INIT_SINK_CALL,1,'init_sink hook'); show(CAVE1,8,'CAVE1')
    show(OPENED_HOOK,1,'opened hook');       show(CAVE2,12,'CAVE2')
    show(UPDEC_HOOK,1,'update_decoder hook'); show(CAVE3,4,'CAVE3')
    print("WROTE",OUT,len(b_),"bytes")
    print("sha1 patched2",hashlib.sha1(bytes(b_)).hexdigest())

main()
