import struct
from capstone import *
from capstone.arm64 import *
from elftools.elf.elffile import ELFFile
from io import BytesIO
f=open('libbluetooth_qti.so','rb'); e=ELFFile(f)
t=e.get_section_by_name('.text'); A=t['sh_addr']; O=t['sh_offset']; S=t['sh_size']
f.seek(0); B=f.read()
dec=open('fable/minidebug.elf','rb').read(); me=ELFFile(BytesIO(dec))
st=me.get_section_by_name('.symtab')
syms=sorted((s['st_value'],s['st_size'],s.name) for s in st.iter_symbols() if s['st_value'] and s['st_info']['type']=='STT_FUNC')
def owner(a):
    best=None
    for v,sz,n in syms:
        if v<=a and (sz==0 or a<v+sz): best=n
    return best
md=Cs(CS_ARCH_ARM64,CS_MODE_ARM); md.detail=True
code=B[O:O+S]
TARGET=0x3f6de1
page={}
reads=[]; writes=[]
for i in md.disasm(code,A):
    if i.mnemonic=='adrp':
        page[i.reg_name(i.operands[0].reg)]=i.operands[1].imm
    elif i.mnemonic in('ldrb','strb') and len(i.operands)==2 and i.operands[1].type==ARM64_OP_MEM:
        base=i.reg_name(i.operands[1].mem.base)
        if base in page:
            va=page[base]+i.operands[1].mem.disp
            if va==TARGET:
                (writes if i.mnemonic=='strb' else reads).append((i.address,owner(i.address)))
print("WRITES to 0x3f6de1 (global peer_sep):")
for a,o in writes: print(f"  {a:#x} in {o}")
print("READS of 0x3f6de1:")
for a,o in reads: print(f"  {a:#x} in {o}")
