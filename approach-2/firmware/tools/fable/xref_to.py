import sys,struct
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
    lo,hi=0,len(syms)
    best=None
    for v,sz,n in syms:
        if v<=a and (sz==0 or a<v+sz): best=(v,n)
    return best
targets=[int(x,16) for x in sys.argv[1:]]
md=Cs(CS_ARCH_ARM64,CS_MODE_ARM); md.detail=True
code=B[O:O+S]
for i in md.disasm(code,A):
    if i.mnemonic in('bl','b') and i.operands and i.operands[-1].type==ARM64_OP_IMM:
        if i.operands[-1].imm in targets:
            o=owner(i.address)
            print(f"{i.address:#x} {i.mnemonic} -> {i.operands[-1].imm:#x}   in {o}")
