#!/usr/bin/env python3
# Native patch for libbluetooth_qti.so (Lenovo TB-J606F, build S320311).
# Makes the A2DP SINK media thread + command queue start even when SINK is the
# SECOND btif_av role (dual A2DP source+sink), by redirecting init_sink's
# call to btif_av_init into a code cave that calls btif_av_init AND
# btif_a2dp_sink_startup (idempotent) while preserving btif_av_init's return value.
# OFFLINE ONLY. Verifies every original byte; refuses on any mismatch.
import sys,struct
from capstone import Cs,CS_ARCH_ARM64,CS_MODE_ARM

SRC='libbluetooth_qti.so'
OUT='fable/libbluetooth_qti.patched.so'

# --- verified addresses (file offset == vaddr for .text) ---
INIT_SINK_CALL = 0x1c11a8      # 'bl btif_av_init' inside init_sink
BTIF_AV_INIT   = 0x1b0b98
SINK_STARTUP   = 0x1a4870
CAVE           = 0x0f5118      # page-align padding 0xf5118..0xf6000, all UDF, unreferenced

ORIG_CALL_WORD = 0x97ffbe7c    # bl btif_av_init  (bytes 7c be ff 97)

def bl(frm,to):
    off=to-frm
    assert off%4==0 and -(1<<27)<=off<(1<<27), "BL out of range"
    imm26=(off>>2)&0x03ffffff
    return 0x94000000|imm26

def main():
    b=bytearray(open(SRC,'rb').read())
    # 1) verify the redirect site
    w=struct.unpack_from('<I',b,INIT_SINK_CALL)[0]
    if w!=ORIG_CALL_WORD:
        sys.exit(f"ABORT: init_sink call site {INIT_SINK_CALL:#x} = {w:#010x}, expected {ORIG_CALL_WORD:#010x}")
    # 2) verify the cave is all UDF(0x00000000) for the bytes we will use (36 bytes)
    CAVE_LEN=0x24
    if any(b[CAVE+i] for i in range(CAVE_LEN)):
        sys.exit(f"ABORT: cave at {CAVE:#x} is not all-zero for {CAVE_LEN} bytes")
    # 3) build cave instructions
    cave=[]
    cave.append(0xa9be7bfd)                       # stp x29,x30,[sp,#-0x20]!
    cave.append(0x910003fd)                       # mov x29,sp
    cave.append(bl(CAVE+0x08, BTIF_AV_INIT))      # bl btif_av_init   (w0=0x12 preserved from caller)
    cave.append(0xb90013e0)                       # str w0,[sp,#0x10]  (save status)
    cave.append(bl(CAVE+0x10, SINK_STARTUP))      # bl btif_a2dp_sink_startup (idempotent)
    cave.append(0xb94013e0)                       # ldr w0,[sp,#0x10]  (restore status)
    cave.append(0xa8c27bfd)                       # ldp x29,x30,[sp],#0x20
    cave.append(0xd65f03c0)                       # ret
    for i,insn in enumerate(cave):
        struct.pack_into('<I',b,CAVE+4*i,insn)
    # 4) redirect init_sink: bl btif_av_init -> bl CAVE
    struct.pack_into('<I',b,INIT_SINK_CALL, bl(INIT_SINK_CALL,CAVE))
    open(OUT,'wb').write(b)
    # 5) verify by disassembly
    md=Cs(CS_ARCH_ARM64,CS_MODE_ARM)
    print("== patched init_sink call site ==")
    for ins in md.disasm(bytes(b[INIT_SINK_CALL:INIT_SINK_CALL+4]),INIT_SINK_CALL):
        print(f"{ins.address:#x}: {ins.bytes.hex()} {ins.mnemonic} {ins.op_str}")
    print("== patched cave ==")
    for ins in md.disasm(bytes(b[CAVE:CAVE+CAVE_LEN]),CAVE):
        tgt=''
        if ins.mnemonic=='bl':
            t=int(ins.op_str[1:],16)
            tgt={BTIF_AV_INIT:'btif_av_init',SINK_STARTUP:'btif_a2dp_sink_startup'}.get(t,'')
        print(f"{ins.address:#x}: {ins.bytes.hex():<10} {ins.mnemonic} {ins.op_str}  {tgt}")
    print("\nWROTE",OUT,len(b),"bytes")
    import hashlib
    print("sha1 orig   ",hashlib.sha1(open(SRC,'rb').read()).hexdigest())
    print("sha1 patched",hashlib.sha1(bytes(b)).hexdigest())

main()
