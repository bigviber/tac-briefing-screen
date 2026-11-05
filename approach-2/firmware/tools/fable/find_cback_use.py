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
code=B[O:O+S]
# find ldr xN,[xM,#0x48] then (soon) blr xN  -> candidate p_app_sink_data_cback calls
insns=list(md.disasm(code,A))
by={i.address:k for k,i in enumerate(insns)}
for k,i in enumerate(insns):
    if i.mnemonic=='ldr' and len(i.operands)==2 and i.operands[1].type==ARM64_OP_MEM and i.operands[1].mem.disp==0x48:
        dst=i.reg_name(i.operands[0].reg)
        # look ahead up to 10 insns for blr dst
        for j in range(k+1,min(k+12,len(insns))):
            jm=insns[j]
            if jm.mnemonic=='blr' and jm.reg_name(jm.operands[0].reg)==dst:
                o=owner(i.address)
                print(f"{i.address:#x} ldr {dst},[..#0x48]; blr @ {jm.address:#x}  in {o}")
                break
