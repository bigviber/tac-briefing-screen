import sys,os,json
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
import fdis as d
for va,sz,lbl in json.loads(sys.argv[1]):
    d.dis2(int(va,16) if isinstance(va,str) else va, int(sz,16) if isinstance(sz,str) else sz, lbl)
