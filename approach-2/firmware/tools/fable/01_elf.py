import sys
from elftools.elf.elffile import ELFFile

f=open('libbluetooth_qti.so','rb')
e=ELFFile(f)
print("machine",e['e_machine'],"class",e.elfclass,"entry",hex(e['e_entry']))
print("=== sections ===")
for s in e.iter_sections():
    print(f"{s.name:20s} type={s['sh_type']:16} addr={s['sh_addr']:#010x} off={s['sh_offset']:#010x} size={s['sh_size']:#010x}")
print("=== segments (PT_LOAD) ===")
for seg in e.iter_segments():
    if seg['p_type']=='PT_LOAD':
        print(f"off={seg['p_offset']:#010x} vaddr={seg['p_vaddr']:#010x} filesz={seg['p_filesz']:#010x} memsz={seg['p_memsz']:#010x} flags={seg['p_flags']}")
