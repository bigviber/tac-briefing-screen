import sys
from io import BytesIO
from capstone import Cs, CS_ARCH_ARM64, CS_MODE_ARM
from capstone.arm64 import ARM64_OP_IMM, ARM64_OP_MEM
from elftools.elf.elffile import ELFFile

LIB, MINI = sys.argv[1], sys.argv[2]
RANGES = [(int(a, 16), int(s, 16)) for a, s in (r.split(':') for r in sys.argv[3:])]
f = open(LIB, 'rb'); e = ELFFile(f)
text = e.get_section_by_name('.text'); TADDR = text['sh_addr']; TOFF = text['sh_offset']
ro = e.get_section_by_name('.rodata'); ROA = ro['sh_addr']; ROO = ro['sh_offset']; ROSZ = ro['sh_size']
f.seek(0); BLOB = f.read()
me = ELFFile(BytesIO(open(MINI, 'rb').read()))
names = {}
for s in me.get_section_by_name('.symtab').iter_symbols():
    if s['st_value'] and s['st_info']['type'] in ('STT_FUNC', 'STT_OBJECT'):
        names.setdefault(s['st_value'], s.name)


def rostr(va):
    if ROA <= va < ROA + ROSZ:
        off = ROO + (va - ROA); end = BLOB.find(b'\0', off)
        return ''.join(c if 32 <= ord(c) < 127 else '.' for c in BLOB[off:end].decode('latin1'))[:90]
    return None


md = Cs(CS_ARCH_ARM64, CS_MODE_ARM); md.detail = True
for va, size in RANGES:
    print('\n==== %s @ %#x size=%#x' % (names.get(va, '?'), va, size))
    page = {}
    for i in md.disasm(BLOB[TOFF + (va - TADDR):TOFF + (va - TADDR) + size], va):
        ann = ''
        ops = i.operands
        if i.mnemonic == 'adrp':
            page[i.reg_name(ops[0].reg)] = ops[1].imm
        elif i.mnemonic == 'add' and len(ops) == 3 and ops[2].type == ARM64_OP_IMM:
            src = i.reg_name(ops[1].reg)
            if src in page:
                t = page[src] + ops[2].imm
                s = rostr(t)
                ann = '  ; "%s"' % s if s is not None else ('  ; &%s' % names[t] if t in names else '  ; &%#x' % t)
                page[i.reg_name(ops[0].reg)] = t
        elif i.mnemonic in ('ldr', 'ldrb', 'str', 'strb') and len(ops) == 2 and ops[1].type == ARM64_OP_MEM:
            b = i.reg_name(ops[1].mem.base)
            if b in page and ops[1].mem.index == 0:
                t = page[b] + ops[1].mem.disp
                ann = '  ; [%#x]%s' % (t, ' ' + names[t] if t in names else '')
        if i.mnemonic in ('bl', 'b') or i.mnemonic.startswith(('b.', 'cb', 'tb')):
            op = ops[-1]
            if op.type == ARM64_OP_IMM and op.imm in names:
                ann = '  -> ' + names[op.imm]
        print('%#010x: %s\t%s%s' % (i.address, i.mnemonic, i.op_str, ann))
