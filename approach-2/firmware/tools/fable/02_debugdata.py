import lzma
from elftools.elf.elffile import ELFFile
from io import BytesIO

f=open('libbluetooth_qti.so','rb')
e=ELFFile(f)
s=e.get_section_by_name('.gnu_debugdata')
data=s.data()
dec=lzma.decompress(data)
open('fable/minidebug.elf','wb').write(dec)
print("decompressed",len(dec),"bytes")
me=ELFFile(BytesIO(dec))
for sec in me.iter_sections():
    print("sec",sec.name,sec['sh_type'],hex(sec['sh_size']))
