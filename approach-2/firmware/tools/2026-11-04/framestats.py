"""Summarise `dumpsys gfxinfo <pkg> framestats` from stdin: per-frame phases in ms."""
import statistics as st
import sys

rows = []
hdr = None
for line in sys.stdin:
    line = line.strip()
    if line.startswith('Flags,'):
        hdr = line.rstrip(',').split(',')
        continue
    if hdr and line and line[0].isdigit() and ',' in line:
        v = line.rstrip(',').split(',')
        if len(v) >= len(hdr):
            rows.append(dict(zip(hdr, map(int, v))))
rows = [r for r in rows if r['Flags'] == 0]
if not rows:
    print('no frames')
    raise SystemExit


def ms(a, b):
    return [(r[b] - r[a]) / 1e6 for r in rows if r[a] and r[b]]


def show(name, vals):
    if vals:
        vals = sorted(vals)
        print('%-22s n=%-4d median %6.1f  p90 %6.1f  max %6.1f' % (name, len(vals), st.median(vals), vals[int(len(vals) * .9)], vals[-1]))


show('total (vsync->done)', ms('IntendedVsync', 'FrameCompleted'))
show('ui anim+layout+draw', ms('HandleInputStart', 'SyncQueued'))
show('sync', ms('SyncStart', 'IssueDrawCommandsStart'))
show('issue draw (cpu)', ms('IssueDrawCommandsStart', 'SwapBuffers'))
show('gpu after swap', ms('SwapBuffers', 'GpuCompleted') if 'GpuCompleted' in hdr else [])
over = sum(1 for v in ms('IntendedVsync', 'FrameCompleted') if v > 16.7)
print('frames over 16.7 ms: %d of %d' % (over, len(rows)))
