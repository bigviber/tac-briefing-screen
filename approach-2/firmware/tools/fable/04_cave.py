from elftools.elf.elffile import ELFFile
f=open('libbluetooth_qti.so','rb'); e=ELFFile(f)
t=e.get_section_by_name('.text'); A=t['sh_addr']; O=t['sh_offset']; S=t['sh_size']
f.seek(0); B=f.read()
import struct
pads={0xd503201f:'nop',0xd4200020:'brk1',0x00000000:'zero'}
# find runs
i=0; runs=[]
words=struct.unpack_from('<%dI'%(S//4), B, O)
n=len(words)
j=0
while j<n:
    w=words[j]
    if w in pads:
        k=j
        while k<n and words[k]==w: k+=1
        length=(k-j)*4
        if length>=24:
            runs.append((A+j*4, length, pads[w]))
        j=k
    else:
        j+=1
runs.sort(key=lambda r:-r[1])
for va,ln,kind in runs[:25]:
    print(f"cave @ {va:#010x} len={ln} ({ln//4} insns) fill={kind}")
print("text end",hex(A+S),"plt at 0x3cf4c0")
