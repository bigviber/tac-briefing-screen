import sys
from capstone import *
from capstone.arm64 import *
from elftools.elf.elffile import ELFFile
from io import BytesIO

f=open('libbluetooth_qti.so','rb'); e=ELFFile(f)
# text: off==addr
text=e.get_section_by_name('.text'); TADDR=text['sh_addr']; TOFF=text['sh_offset']; TSZ=text['sh_size']
rodata=e.get_section_by_name('.rodata'); ROA=rodata['sh_addr']; ROO=rodata['sh_offset']; ROSZ=rodata['sh_size']
f.seek(0); BLOB=f.read()
# symbol map
dec=open('fable/minidebug.elf','rb').read(); me=ELFFile(BytesIO(dec))
st=me.get_section_by_name('.symtab')
addr2name={}
for s in st.iter_symbols():
    if s['st_value'] and s['st_info']['type'] in ('STT_FUNC','STT_OBJECT'):
        addr2name.setdefault(s['st_value'],s.name)
def demang(n):
    return n
def getbytes(vaddr,size):
    off=TOFF+(vaddr-TADDR)
    return BLOB[off:off+size]
def rostr(vaddr):
    if ROA<=vaddr<ROA+ROSZ:
        off=ROO+(vaddr-ROA); end=BLOB.find(b'\x00',off)
        return BLOB[off:end].decode('latin1')
    return None
md=Cs(CS_ARCH_ARM64,CS_MODE_ARM); md.detail=True
def dis(vaddr,size,label=''):
    print(f"\n==== {label} @ {vaddr:#x} size={size:#x} ====")
    code=getbytes(vaddr,size)
    adrp={}
    for i in md.disasm(code,vaddr):
        ann=''
        # track adrp
        if i.mnemonic=='adrp':
            adrp[i.operands[1].imm and i.reg_name(i.operands[0].reg)]=i.operands[1].imm
            adrp[i.reg_name(i.operands[0].reg)]=i.operands[1].imm
        if i.mnemonic in('add','ldr') and len(i.operands)>=2:
            # resolve adrp+add page+off -> string
            pass
        if i.mnemonic in ('bl','b','cbz','cbnz','tbz','tbnz','b.ne','b.eq','b.gt','b.lt','b.ge','b.le','b.hi','b.ls','b.cs','b.cc'):
            op=i.operands[-1]
            if op.type==ARM64_OP_IMM:
                t=op.imm
                if t in addr2name: ann=f"  -> {addr2name[t]}"
                else: ann=f"  -> sub_{t:x}"
        print(f"{i.address:#010x}: {i.bytes.hex():<12} {i.mnemonic}\t{i.op_str}{ann}")
# resolve adrp+add strings in a second pass
def dis2(vaddr,size,label=''):
    print(f"\n==== {label} @ {vaddr:#x} size={size:#x} ====")
    code=getbytes(vaddr,size)
    insns=list(md.disasm(code,vaddr))
    pagereg={}
    for i in insns:
        ann=''
        if i.mnemonic=='adrp':
            pagereg[i.reg_name(i.operands[0].reg)]=i.operands[1].imm
        elif i.mnemonic=='add' and len(i.operands)==3 and i.operands[2].type==ARM64_OP_IMM:
            src=i.reg_name(i.operands[1].reg)
            if src in pagereg:
                va=pagereg[src]+i.operands[2].imm
                s=rostr(va)
                if s is not None:
                    s=''.join(c if 32<=ord(c)<127 else '.' for c in s)
                    ann=f'  ; "{s}"'
                elif va in addr2name: ann=f"  ; &{addr2name[va]}"
                pagereg[i.reg_name(i.operands[0].reg)]=va
        elif i.mnemonic=='ldr' and len(i.operands)==2 and i.operands[1].type==ARM64_OP_MEM:
            base=i.reg_name(i.operands[1].mem.base)
            if base in pagereg:
                va=pagereg[base]+i.operands[1].mem.disp
                if va in addr2name: ann=f"  ; [&{addr2name[va]}]"
        if i.mnemonic in ('bl','b') or i.mnemonic.startswith('b.') or i.mnemonic in('cbz','cbnz','tbz','tbnz'):
            op=i.operands[-1]
            if op.type==ARM64_OP_IMM:
                t=op.imm
                if t in addr2name: ann+=f"  -> {addr2name[t]}"
                elif TADDR<=t<TADDR+TSZ and not (vaddr<=t<vaddr+size): ann+=f"  -> sub_{t:x}"
        line=f"{i.address:#010x}: {i.bytes.hex():<10} {i.mnemonic:8s}{i.op_str}{ann}"
        sys.stdout.buffer.write((line+"\n").encode('ascii','replace'))

if __name__=='__main__':
    import json
    targets=json.loads(sys.argv[1])
    for va,sz,lbl in targets:
        dis2(va,sz,lbl)
