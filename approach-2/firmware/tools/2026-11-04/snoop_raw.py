import struct,sys
d=open(sys.argv[1],'rb').read(); pos=16; chan={}; pend={}; k=0; t0=None
while pos+24<=len(d):
    ol,il,fl,dr,ts=struct.unpack('>IIIIq',d[pos:pos+24]); p=d[pos+24:pos+24+il]; pos+=24+il
    if not p or p[0]!=2 or len(p)<9: continue
    h=struct.unpack('<H',p[1:3])[0]; hd=h&0xfff; b=p[5:]; rx=bool(fl&1)
    ln,cid=struct.unpack('<HH',b[:4]); pl=b[4:]
    if cid==1 and len(pl)>=4:
        if pl[0]==2 and len(pl)>=8:
            psm,sc=struct.unpack('<HH',pl[4:8]); pend[(hd,sc,rx)]=psm
        elif pl[0]==3 and len(pl)>=12:
            dc,sc,res=struct.unpack('<HHH',pl[4:10]); psm=pend.get((hd,sc,not rx))
            if psm and res==0: chan[(hd,dc,rx)]=psm; chan[(hd,sc,not rx)]=psm
        continue
    if (chan.get((hd,cid,rx)) or chan.get((hd,cid,not rx)))!=0x17: continue
    if t0 is None: t0=ts
    if k<8: print('%7.3f %s %s'%((ts-t0)/1e6,'RX iPhone->tablet' if rx else 'TX tablet->iPhone',pl.hex(' ')))
    k+=1
