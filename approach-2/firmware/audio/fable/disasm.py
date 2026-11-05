# Thumb-2 disassembler with literal/string/PLT annotation for 32-bit ARM shared objects.
import sys, struct, lzma, io
from elftools.elf.elffile import ELFFile
from elftools.elf.sections import SymbolTableSection
from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB, CS_MODE_ARM
from capstone.arm import ARM_OP_IMM, ARM_OP_MEM, ARM_OP_REG, ARM_REG_PC

path = sys.argv[1]
funcs = sys.argv[2:]   # name or name=0xaddr:size
data = open(path, 'rb').read()
elf = ELFFile(io.BytesIO(data))

secs = {s.name: s for s in elf.iter_sections()}


def vaddr_to_off(v):
    for seg in elf.iter_segments():
        if seg['p_type'] == 'PT_LOAD' and seg['p_vaddr'] <= v < seg['p_vaddr'] + seg['p_filesz']:
            return v - seg['p_vaddr'] + seg['p_offset']
    return None


def rd32(v):
    o = vaddr_to_off(v)
    return struct.unpack_from('<I', data, o)[0] if o is not None else None


def rdstr(v, maxlen=160):
    o = vaddr_to_off(v)
    if o is None:
        return None
    end = data.find(b'\0', o, o + maxlen)
    if end < 0:
        return None
    s = data[o:end]
    if len(s) == 0:
        return ''
    if all(32 <= c < 127 or c in (9, 10, 13) for c in s):
        return s.decode()
    return None


syms = {}
funcmap = {}


def addsyms(e):
    for s in e.iter_sections():
        if isinstance(s, SymbolTableSection):
            for sym in s.iter_symbols():
                if sym.name and sym['st_value']:
                    syms.setdefault(sym['st_value'] & ~1, sym.name)
                    syms.setdefault(sym['st_value'], sym.name)
                if sym['st_info']['type'] == 'STT_FUNC' and sym.name:
                    funcmap[sym.name] = (sym['st_value'] & ~1, sym['st_size'])


addsyms(elf)
if '.gnu_debugdata' in secs:
    mini = ELFFile(io.BytesIO(lzma.decompress(secs['.gnu_debugdata'].data())))
    addsyms(mini)

plt = {}
relplt = secs.get('.rel.plt')
if relplt:
    symtab = elf.get_section(relplt['sh_link'])
    got2name = {r['r_offset']: symtab.get_symbol(r['r_info_sym']).name for r in relplt.iter_relocations()}
    pltsec = secs['.plt']
    base = pltsec['sh_addr']
    pdata = pltsec.data()
    md_arm = Cs(CS_ARCH_ARM, CS_MODE_ARM)
    md_arm.detail = True
    i = 0
    while i + 12 <= len(pdata):
        ins = list(md_arm.disasm(pdata[i:i + 12], base + i))
        ok = False
        if len(ins) == 3 and ins[0].mnemonic == 'add' and ins[1].mnemonic == 'add' and ins[2].mnemonic == 'ldr':
            try:
                imm1 = ins[0].operands[2].imm
                imm2 = ins[1].operands[2].imm
                imm3 = ins[2].operands[1].mem.disp
                got = base + i + 8 + imm1 + imm2 + imm3
                if got in got2name:
                    plt[base + i] = got2name[got]
                    ok = True
            except Exception:
                pass
        i += 12 if ok else 4


def symname(v):
    v2 = v & ~1
    if v in plt:
        return 'PLT:' + plt[v]
    if v2 in plt:
        return 'PLT:' + plt[v2]
    if v2 in syms:
        return syms[v2]
    if v in syms:
        return syms[v]
    return None


md = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
md.detail = True

for spec in funcs:
    if '=' in spec:
        name, rng = spec.split('=')
        a, sz = rng.split(':')
        start = int(a, 16)
        size = int(sz, 0)
    else:
        name = spec
        start, size = funcmap[name]
    off = vaddr_to_off(start)
    code = data[off:off + size]
    print("\n==== %s @ 0x%x size %d ====" % (name, start, size))
    regs = {}
    for ins in md.disasm(code, start):
        note = ''
        ops = ins.operands
        mn = ins.mnemonic
        if mn.startswith('ldr') and len(ops) == 2 and ops[1].type == ARM_OP_MEM and ops[1].mem.base == ARM_REG_PC:
            lit = ((ins.address + 4) & ~3) + ops[1].mem.disp
            val = rd32(lit)
            if val is not None and mn == 'ldr':
                regs[ins.reg_name(ops[0].reg)] = val
                note = '; lit=0x%x' % val
                s = symname(val)
                if s:
                    note += ' (%s)' % s
        elif mn == 'add' and len(ops) == 2 and ops[1].type == ARM_OP_REG and ops[1].reg == ARM_REG_PC:
            r = ins.reg_name(ops[0].reg)
            if r in regs:
                addr = (regs[r] + ins.address + 4) & 0xffffffff
                regs[r] = addr
                note = '; -> 0x%x' % addr
                s = rdstr(addr)
                sn = symname(addr)
                if sn:
                    note += ' %s' % sn
                if s:
                    note += ' "%s"' % s
        elif mn == 'movw' and ops[1].type == ARM_OP_IMM:
            regs[ins.reg_name(ops[0].reg)] = ops[1].imm
        elif mn == 'movt' and ops[1].type == ARM_OP_IMM:
            r = ins.reg_name(ops[0].reg)
            regs[r] = (regs.get(r, 0) & 0xffff) | (ops[1].imm << 16)
            note = '; = 0x%x' % regs[r]
        elif mn.startswith('b') or mn.startswith('cb'):
            for op in ops:
                if op.type == ARM_OP_IMM:
                    s = symname(op.imm)
                    if s:
                        note = '; ' + s
        elif mn.startswith('mov') and len(ops) == 2 and ops[1].type == ARM_OP_IMM:
            regs[ins.reg_name(ops[0].reg)] = ops[1].imm
        elif mn.startswith('ldr') and len(ops) == 2 and ops[1].type == ARM_OP_MEM:
            b = ins.reg_name(ops[1].mem.base)
            if b in regs and ops[1].mem.index == 0:
                a = regs[b] + ops[1].mem.disp
                sn = symname(a)
                note = '; [0x%x%s]' % (a, ' ' + sn if sn else '')
            regs.pop(ins.reg_name(ops[0].reg), None)
        elif mn.startswith('str') and len(ops) == 2 and ops[1].type == ARM_OP_MEM:
            b = ins.reg_name(ops[1].mem.base)
            if b in regs and ops[1].mem.index == 0:
                a = regs[b] + ops[1].mem.disp
                sn = symname(a)
                note = '; [0x%x%s]' % (a, ' ' + sn if sn else '')
        else:
            if ops and ops[0].type == ARM_OP_REG and mn not in ('cmp', 'tst', 'push', 'pop', 'stm', 'stmdb', 'stmia', 'cmn', 'teq'):
                regs.pop(ins.reg_name(ops[0].reg), None)
        print("%06x: %-8s %-34s %s" % (ins.address, mn, ins.op_str, note))
