from elftools.elf.elffile import ELFFile
from io import BytesIO
dec=open('fable/minidebug.elf','rb').read()
me=ELFFile(BytesIO(dec))
st=me.get_section_by_name('.symtab')
syms=[]
for sym in st.iter_symbols():
    if sym['st_value'] and sym['st_info']['type']=='STT_FUNC':
        syms.append((sym['st_value'], sym['st_size'], sym.name))
syms.sort()
want=['btif_av_init','init_sink','init_src','btif_a2dp_sink_startup','btif_a2dp_source_startup',
      'btif_av_sink_execute_service','btif_enable_service','btif_a2dp_sink_worker_thread',
      'btif_a2dp_sink_set_focus_state_req','btif_a2dp_sink_command_ready','fixed_queue_enqueue',
      'fixed_queue_new','btif_a2dp_sink_init','btif_av_source_execute_service','bta_av_enable',
      'BTA_AvEnable','btif_a2dp_sink_startup','btif_a2dp_sink_cleanup','cleanup_sink',
      'btif_a2dp_sink_set_focus_state_event']
byname={}
for v,sz,n in syms:
    byname.setdefault(n,[]).append((v,sz))
import json
out=open('fable/symtab_funcs.txt','w')
for v,sz,n in syms:
    out.write(f"{v:#010x} {sz:#8x} {n}\n")
out.close()
print("total func syms:",len(syms))
for w in want:
    if w in byname:
        for v,sz in byname[w]:
            print(f"  {v:#010x} size={sz:#x} {w}")
    else:
        print(f"  MISSING {w}")
