import struct
from elftools.elf.elffile import ELFFile
f=open('libbluetooth_qti.so','rb'); e=ELFFile(f); f.seek(0); B=f.read()
CAVE_LO=0xf5118; CAVE_HI=0xf6000
hits=[]
for secname in ['.data.rel.ro','.got','.got.plt','.data','.rodata']:
    s=e.get_section_by_name(secname)
    if not s: continue
    off=s['sh_offset']; sz=s['sh_size']; base=s['sh_addr']
    data=B[off:off+sz]
    for k in range(0,sz-7,8):
        v=struct.unpack_from('<Q',data,k)[0]
        if CAVE_LO<=v<CAVE_HI:
            hits.append((secname,hex(base+k),hex(v)))
print("8-byte pointers into cave:",len(hits))
for h in hits[:30]: print(h)
# relocations
for relname in ['.rela.dyn','.rela.plt']:
    s=e.get_section_by_name(relname)
    if not s: continue
    from elftools.elf.relocation import RelocationSection
    if isinstance(s,RelocationSection):
        for r in s.iter_relocations():
            add=r['r_addend']
            if CAVE_LO<=add<CAVE_HI:
                print("reloc addend into cave",relname,hex(add))
print("done")
