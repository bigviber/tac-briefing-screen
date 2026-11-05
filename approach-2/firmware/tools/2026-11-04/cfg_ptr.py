import sys,struct
from io import BytesIO
from capstone import Cs,CS_ARCH_ARM64,CS_MODE_ARM
from capstone.arm64 import ARM64_OP_IMM,ARM64_OP_MEM
from elftools.elf.elffile import ELFFile
LIB,MINI=sys.argv[1],sys.argv[2]
f=open(LIB,'rb');e=ELFFile(f);f.seek(0);B=f.read()
def sec_of(va):
    for s in e.iter_sections():
        if s['sh_addr'] and s['sh_addr']<=va<s['sh_addr']+s['sh_size']: return s
def rd(va,n):
    s=sec_of(va)
    if s is None or s['sh_type']=='SHT_NOBITS': return None
    return B[s['sh_offset']+va-s['sh_addr']:s['sh_offset']+va-s['sh_addr']+n]
PTR=0x3def20
print('p_bta_av_cfg var at %#x in section %s'%(PTR,sec_of(PTR).name), 'raw', rd(PTR,8).hex() if rd(PTR,8) else None)
# relocation for the pointer
for sec in e.iter_sections():
    if sec.header['sh_type'] in('SHT_RELA','SHT_ANDROID_RELA') and hasattr(sec,'iter_relocations'):
        for r in sec.iter_relocations():
            if r['r_offset']==PTR: print('reloc',sec.name,hex(r['r_addend']))
text=e.get_section_by_name('.text');T=text['sh_addr'];O=text['sh_offset'];N=text['sh_size']
md=Cs(CS_ARCH_ARM64,CS_MODE_ARM);md.detail=False
code=B[O:O+N]
insns=[]
targets={}
# scan linear for adrp x,#0x3de000 followed within 12 insns by str/ldr [x,#0xf20]
import re
i=0
words=struct.unpack('<%dI'%(len(code)//4),code[:len(code)//4*4])
def adrp_target(w,pc):
    if (w&0x9f000000)!=0x90000000: return None
    imm=((w>>29)&3)|(((w>>5)&0x7ffff)<<2)
    if imm&(1<<20): imm-=1<<21
    return (pc&~0xfff)+(imm<<12)
hits=[]
for k,w in enumerate(words):
    pc=T+k*4
    if adrp_target(w,pc)==0x3de000:
        rd_=w&31
        for j in range(1,16):
            w2=words[k+j]
            # str x, [rn,#imm] 64-bit: 0xF9000000 ; ldr 0xF9400000
            if (w2&0xffc00000)==0xf9000000 and ((w2>>5)&31)==rd_ and ((w2>>10)&0xfff)*8==0xf20:
                hits.append(('STR',T+(k+j)*4,w2&31,k))
            if (w2&0xffc00000)==0xf9400000 and ((w2>>5)&31)==rd_ and ((w2>>10)&0xfff)*8==0xf20:
                hits.append(('LDR',T+(k+j)*4,w2&31,k))
seen=set()
for h in hits:
    if h[1] in seen: continue
    seen.add(h[1]); print(h[0],hex(h[1]),'x%d'%h[2])
me=ELFFile(BytesIO(open(MINI,'rb').read()))
funcs=sorted((s['st_value'],s['st_size'],s.name) for s in me.get_section_by_name('.symtab').iter_symbols() if s['st_info']['type']=='STT_FUNC' and s['st_value'])
import bisect
keys=[x[0] for x in funcs]
for h in sorted(seen):
    p=bisect.bisect_right(keys,h)-1
    print(hex(h),'in',funcs[p][2],hex(funcs[p][0]))
