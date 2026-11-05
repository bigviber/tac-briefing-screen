"""Redact bt_config.conf: bonded devices only, every key value and name removed, addresses replaced. Everything else stays."""
import re
import sys

src, dst = sys.argv[1], sys.argv[2]
NAMED = {'AA:00:00:00:00:01': 'BB:00:00:00:00:16', 'AA:00:00:00:00:02': 'BB:00:00:00:00:17'}
for a in sys.argv[3:]:
    real, ph = a.split('=')
    NAMED[real.upper()] = ph
MAC = re.compile(r'\b[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}\b')
others = {}


def sub(m):
    v = m.group(0).upper()
    if v in ('00:00:00:00:00:00', 'FF:FF:FF:FF:FF:FF'):
        return m.group(0)
    if v in NAMED:
        return NAMED[v]
    others.setdefault(v, 'BB:00:00:01:00:%02X' % (len(others) + 1))
    return others[v]


sections = []
cur = None
for line in open(src, encoding='latin1'):
    line = line.rstrip('\r\n')
    if line.startswith('['):
        cur = [line, []]
        sections.append(cur)
    elif cur is not None:
        cur[1].append(line)
    else:
        sections.append(['', [line]])
        cur = sections[-1]
out = []
kept = dropped = redacted = 0
for head, body in sections:
    is_dev = bool(MAC.fullmatch(head.strip('[]'))) if head else False
    bonded = any(re.match(r'(LinkKey|LE_KEY_)', ln) for ln in body)
    if is_dev and not bonded:     # devices merely seen nearby
        dropped += 1
        continue
    kept += 1
    out.append(MAC.sub(sub, head))
    for ln in body:
        k = ln.split('=')[0].strip()
        if k != 'LinkKeyType' and re.search(r'(?i)key|irk|salt|\bdhk\b|\ber\b|\bir\b', k):
            out.append(k + ' = <redacted>')
            redacted += 1
        elif k in ('Name', 'Aliase', 'Alias', 'RemoteName') and head != '[Adapter]':
            out.append(k + ' = <redacted>')
            redacted += 1
        else:
            out.append(MAC.sub(sub, ln))
open(dst, 'w', encoding='utf-8', newline='\n').write('\n'.join(out) + '\n')
print('sections kept', kept, 'nearby dropped', dropped, 'values redacted', redacted)
