import struct
from capstone import *
from capstone.arm64 import *
from elftools.elf.elffile import ELFFile
f=open('libbluetooth_qti.so','rb'); e=ELFFile(f)
t=e.get_section_by_name('.text'); A=t['sh_addr']; O=t['sh_offset']; S=t['sh_size']
f.seek(0); B=f.read()
CAVE_LO=0xf5118; CAVE_HI=0xf6000
md=Cs(CS_ARCH_ARM64,CS_MODE_ARM); md.detail=True
code=B[O:O+S]
hits=[]
# direct branches
for i in md.disasm(code,A):
    if i.id==0: continue
    for op in i.operands:
        if op.type==ARM64_OP_IMM and (i.mnemonic in('b','bl','cbz','cbnz','tbz','tbnz') or i.mnemonic.startswith('b.')):
            if CAVE_LO<=op.imm<CAVE_HI:
                hits.append((i.address,i.mnemonic,op.imm))
print("direct branch hits into cave:",len(hits))
for h in hits[:20]: print(hex(h[0]),h[1],hex(h[2]))
# adrp+add computed addresses (page granularity) - check any adrp page == 0xf5000 then add lands in range
# quick: scan adrp pairs
prev={}
addr_hits=[]
for i in md.disasm(code,A):
    if i.mnemonic=='adrp':
        prev[i.reg_name(i.operands[0].reg)]=i.operands[1].imm
    elif i.mnemonic=='add' and len(i.operands)==3 and i.operands[2].type==ARM64_OP_IMM:
        src=i.reg_name(i.operands[1].reg)
        if src in prev:
            va=prev[src]+i.operands[2].imm
            if CAVE_LO<=va<CAVE_HI:
                addr_hits.append((i.address,hex(va)))
print("adrp+add computed addrs into cave:",len(addr_hits))
for h in addr_hits[:20]: print(hex(h[0]),h[1])
