import struct
from capstone import *; from capstone.arm64 import *
from elftools.elf.elffile import ELFFile; from io import BytesIO
f=open('libbluetooth_qti.so','rb'); e=ELFFile(f)
t=e.get_section_by_name('.text'); A=t['sh_addr']; O=t['sh_offset']; S=t['sh_size']; f.seek(0); B=f.read()
dec=open('fable/minidebug.elf','rb').read(); me=ELFFile(BytesIO(dec)); st=me.get_section_by_name('.symtab')
syms=sorted((s['st_value'],s['st_size'],s.name) for s in st.iter_symbols() if s['st_value'] and s['st_info']['type']=='STT_FUNC')
def owner(a):
    best=None
    for v,sz,n in syms:
        if v<=a and (sz==0 or a<v+sz): best=n
    return best
md=Cs(CS_ARCH_ARM64,CS_MODE_ARM); md.detail=True
# find strb with disp 0x12 (peer_sep per-index) in btif_av range
for i in md.disasm(B[O:O+S],A):
    if i.mnemonic=='strb' and len(i.operands)==2 and i.operands[1].type==ARM64_OP_MEM and i.operands[1].mem.disp==0x12:
        o=owner(i.address)
        if o and ('btif_av' in o or 'bta_av' in o):
            print(f"{i.address:#x}  strb {i.reg_name(i.operands[0].reg)},[..#0x12]  in {o}")
