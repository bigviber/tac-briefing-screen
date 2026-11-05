import sys, lzma, io
from elftools.elf.elffile import ELFFile
from elftools.elf.relocation import RelocationSection
from elftools.elf.sections import SymbolTableSection

path = sys.argv[1]
f = open(path, 'rb')
elf = ELFFile(f)
print("== sections ==")
for s in elf.iter_sections():
    print("%-20s addr=0x%06x off=0x%06x size=0x%06x" % (s.name, s['sh_addr'], s['sh_offset'], s['sh_size']))

dynsym = elf.get_section_by_name('.dynsym')
print("== dynsym UND (imports) ==")
for sym in dynsym.iter_symbols():
    if sym['st_shndx'] == 'SHN_UNDEF' and sym.name:
        print("  IMPORT", sym.name)
print("== dynsym defined (exports) ==")
for sym in dynsym.iter_symbols():
    if sym['st_shndx'] != 'SHN_UNDEF' and sym.name and sym['st_info']['type'] in ('STT_FUNC','STT_OBJECT'):
        print("  EXPORT %-30s 0x%06x size=%d %s" % (sym.name, sym['st_value'], sym['st_size'], sym['st_info']['type']))

print("== rel.plt ==")
relplt = elf.get_section_by_name('.rel.plt')
if relplt:
    symtab = elf.get_section(relplt['sh_link'])
    for r in relplt.iter_relocations():
        sym = symtab.get_symbol(r['r_info_sym'])
        print("  GOT 0x%06x -> %s" % (r['r_offset'], sym.name))

gdd = elf.get_section_by_name('.gnu_debugdata')
if gdd:
    data = lzma.decompress(gdd.data())
    mini = ELFFile(io.BytesIO(data))
    print("== gnu_debugdata symbols ==")
    for s in mini.iter_sections():
        if isinstance(s, SymbolTableSection):
            for sym in s.iter_symbols():
                if sym['st_info']['type'] in ('STT_FUNC','STT_OBJECT') or sym.name:
                    print("  %-34s 0x%06x size=%-5d %s shndx=%s" % (sym.name, sym['st_value'], sym['st_size'], sym['st_info']['type'], sym['st_shndx']))
