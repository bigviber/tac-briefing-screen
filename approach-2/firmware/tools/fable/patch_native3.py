#!/usr/bin/env python3
# Combined native patch v3 for libbluetooth_qti.so (Lenovo TB-J606F, S320311).
# From the ORIGINAL .so:
#  v1  init_sink -> CAVE1: run btif_a2dp_sink_startup (sink worker/queue) AND, via
#      CAVE4, (v3) reissue the sink BTA_AvRegister that dual mode skips.
#  v2a opened_handler -> CAVE2: sink-enabled gate killing the 3s remote-start suspend.
#  v2b update_decoder -> CAVE3: clear rx_flush on sink codec config.
#  v3  CAVE4: when the SOURCE media task is running (=dual mode, sink was the 2nd
#      btif_av role and its registration was skipped), call BTA_AvRegister(
#      BTA_AV_CHNL_AUDIO, "Advanced Audio Sink", 0, &bte_av_sink_media_callback,
#      UUID_SERVCLASS_AUDIO_SINK) max_conn times -> creates SNK SEPs (tsep=1) whose
#      p_app_sink_data_cback == bte_av_sink_media_callback, so the phone binds a real
#      sink SEP and BTA delivers CFG(0x14)/DATA(0x15) to the sink callback -> decode.
# Verifies every original byte; refuses on mismatch. Verifies result by disasm.
import sys,struct,hashlib
from capstone import Cs,CS_ARCH_ARM64,CS_MODE_ARM

SRC='libbluetooth_qti.so'; OUT='fable/libbluetooth_qti.patched3.so'

INIT_SINK_CALL=0x1c11a8; ORIG_CALL =0x97ffbe7c   # bl btif_av_init
OPENED_HOOK   =0x1b6be4; ORIG_OPENED=0x39000268  # strb w8,[x19]
UPDEC_HOOK    =0x1a4f70; ORIG_UPDEC =0xd100c3ff   # sub sp,sp,#0x30
BTIF_AV_INIT=0x1b0b98; SINK_STARTUP=0x1a4870; IS_SINK_ENABLED=0x1be6d4
SRC_TASK_RUN=0x1a774c; BTA_AVREGISTER=0x126710
OPENED_CONT=0x1b6be8; OPENED_SKIP=0x1b6bf8; UPDEC_CONT=0x1a4f74

CAVE1=0x0f5118; CAVE2=0x0f513c; CAVE3=0x0f516c; CAVE4=0x0f517c

def bl(frm,to): off=to-frm; assert -(1<<27)<=off<(1<<27); return 0x94000000|((off>>2)&0x03ffffff)
def b (frm,to): off=to-frm; assert -(1<<27)<=off<(1<<27); return 0x14000000|((off>>2)&0x03ffffff)
def cbnz_w(r,frm,to): off=to-frm; assert -(1<<20)<=off<(1<<20); return 0x35000000|(((off>>2)&0x7ffff)<<5)|r
def cbz_w (r,frm,to): off=to-frm; assert -(1<<20)<=off<(1<<20); return 0x34000000|(((off>>2)&0x7ffff)<<5)|r
def blt  (frm,to): off=to-frm; assert -(1<<20)<=off<(1<<20); return 0x54000000|(((off>>2)&0x7ffff)<<5)|0xb  # cond=LT
def adrp(frm,page,reg): off=(page&~0xfff)-(frm&~0xfff); imm=off>>12; return 0x90000000|((imm&3)<<29)|(((imm>>2)&0x7ffff)<<5)|reg

def main():
    bb=bytearray(open(SRC,'rb').read())
    rd=lambda a: struct.unpack_from('<I',bb,a)[0]
    wr=lambda a,w: struct.pack_into('<I',bb,a,w)
    for a,exp,nm in [(INIT_SINK_CALL,ORIG_CALL,'init_sink bl'),(OPENED_HOOK,ORIG_OPENED,'opened strb'),(UPDEC_HOOK,ORIG_UPDEC,'updec sub')]:
        if rd(a)!=exp: sys.exit(f"ABORT {nm}@{a:#x}: {rd(a):#010x}!={exp:#010x}")
    for i in range(CAVE1,CAVE4+0x70):
        if bb[i]!=0: sys.exit(f"ABORT cave byte {i:#x} not zero")
    # CAVE1 (v1 + call CAVE4)
    c1=[0xa9be7bfd,                    # stp x29,x30,[sp,#-0x20]!
        0x910003fd,                    # mov x29,sp
        bl(CAVE1+0x08,BTIF_AV_INIT),   # bl btif_av_init  (w0=0x12)
        0xb90013e0,                    # str w0,[sp,#0x10]
        bl(CAVE1+0x10,SINK_STARTUP),   # bl btif_a2dp_sink_startup
        bl(CAVE1+0x14,CAVE4),          # bl CAVE4  (v3: reissue sink registration)
        0xb94013e0,                    # ldr w0,[sp,#0x10]
        0xa8c27bfd,                    # ldp x29,x30,[sp],#0x20
        0xd65f03c0]                    # ret
    # CAVE2 (v2a)
    c2=[0xa9be53f3,0xa90163f7,bl(CAVE2+0x08,IS_SINK_ENABLED),0x2a0003e9,
        0xa94163f7,0xa8c253f3,cbnz_w(9,CAVE2+0x18,CAVE2+0x2c),0x52800028,
        0x39000268,0x52801809,b(CAVE2+0x28,OPENED_CONT),b(CAVE2+0x2c,OPENED_SKIP)]
    # CAVE3 (v2b)
    c3=[adrp(CAVE3+0x00,0x3f3000,16),0x392fa21f,ORIG_UPDEC,b(CAVE3+0x0c,UPDEC_CONT)]
    # CAVE4 (v3): source-gated sink registration loop (do-while, >=1)
    L=CAVE4
    c4=[0xa9bc7bfd,                    # +00 stp x29,x30,[sp,#-0x40]!
        0x910003fd,                    # +04 mov x29,sp
        0xa90153f3,                    # +08 stp x19,x20,[sp,#0x10]
        0xa9025ff6,                    # +0c stp x22,x23,[sp,#0x20]
        bl(L+0x10,SRC_TASK_RUN),       # +10 bl btif_a2dp_source_media_task_is_running
        cbz_w(0,L+0x14,L+0x54),        # +14 cbz w0,.done   (sink-only: skip)
        adrp(L+0x18,0x3db000,22),      # +18 adrp x22,0x3db000
        adrp(L+0x1c,0x65000,19),       # +1c adrp x19,0x65000
        0x91036e73,                    # +20 add x19,x19,#0xdb  ("Advanced Audio Sink")
        adrp(L+0x24,0x3cc000,20),      # +24 adrp x20,0x3cc000
        0x913e2294,                    # +28 add x20,x20,#0xf88 (&bte_av_sink_media_callback)
        0x2a1f03f7,                    # +2c mov w23,wzr
        # .loop (+30):
        0x52800800,                    # +30 mov w0,#0x40
        0x52822164,                    # +34 mov w4,#0x110b
        0xaa1303e1,                    # +38 mov x1,x19
        0x2a1f03e2,                    # +3c mov w2,wzr
        0xaa1403e3,                    # +40 mov x3,x20
        0x110006f7,                    # +44 add w23,w23,#1
        bl(L+0x48,BTA_AVREGISTER),     # +48 bl BTA_AvRegister
        0xb94f32c8,                    # +4c ldr w8,[x22,#0xf30]
        0x6b0802ff,                    # +50 cmp w23,w8
        blt(L+0x54,L+0x30),            # +54 ... wait placement; recompute below
        ]
    # fix: the b.lt is at +54 but .done must follow; rebuild c4 with correct offsets
    c4=[0xa9bc7bfd,0x910003fd,0xa90153f3,0xa9025ff6,
        bl(L+0x10,SRC_TASK_RUN),cbz_w(0,L+0x14,L+0x58),
        adrp(L+0x18,0x3db000,22),adrp(L+0x1c,0x65000,19),0x91036e73,
        adrp(L+0x24,0x3cc000,20),0x913e2294,0x2a1f03f7,
        0x52800800,0x52822164,0xaa1303e1,0x2a1f03e2,0xaa1403e3,0x110006f7,
        bl(L+0x48,BTA_AVREGISTER),0xb94f32c8,0x6b0802ff,blt(L+0x54,L+0x30),
        # .done (+58):
        0xa9425ff6,0xa94153f3,0xa8c47bfd,0xd65f03c0]
    for cave,base in [(c1,CAVE1),(c2,CAVE2),(c3,CAVE3),(c4,CAVE4)]:
        for i,w in enumerate(cave): wr(base+4*i,w)
    wr(INIT_SINK_CALL,bl(INIT_SINK_CALL,CAVE1))
    wr(OPENED_HOOK,b(OPENED_HOOK,CAVE2))
    wr(UPDEC_HOOK,b(UPDEC_HOOK,CAVE3))
    open(OUT,'wb').write(bb)
    md=Cs(CS_ARCH_ARM64,CS_MODE_ARM)
    names={BTIF_AV_INIT:'btif_av_init',SINK_STARTUP:'sink_startup',CAVE4:'CAVE4',IS_SINK_ENABLED:'is_sink_enabled',
           SRC_TASK_RUN:'src_task_running',BTA_AVREGISTER:'BTA_AvRegister',OPENED_CONT:'opened_cont',OPENED_SKIP:'opened_skip',UPDEC_CONT:'updec_cont'}
    def show(a,n,lbl):
        print(f"-- {lbl} @ {a:#x} --")
        for ins in md.disasm(bytes(bb[a:a+n*4]),a):
            t=''
            if ins.mnemonic in('bl','b') or ins.mnemonic.startswith('b.') or ins.mnemonic in('cbz','cbnz'):
                try: t=names.get(int(ins.op_str.split('#')[-1],16),'')
                except: pass
            print(f"  {ins.address:#08x}: {ins.bytes.hex():<10} {ins.mnemonic:7s}{ins.op_str}  {t}")
    for a,n,l in [(INIT_SINK_CALL,1,'init_sink hook'),(CAVE1,9,'CAVE1'),(OPENED_HOOK,1,'opened hook'),
                  (CAVE2,12,'CAVE2'),(UPDEC_HOOK,1,'updec hook'),(CAVE3,4,'CAVE3'),(CAVE4,26,'CAVE4')]:
        show(a,n,l)
    print("WROTE",OUT,len(bb),"bytes; sha1",hashlib.sha1(bytes(bb)).hexdigest())

main()
