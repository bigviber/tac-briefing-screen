#!/usr/bin/env python3
# Java-gate patch (no Xposed). Edits com.android.bluetooth.btservice.Config.addAudioProfiles
# so NEITHER A2dpService nor A2dpSinkService is dropped -> both profiles start.
# Two 2-byte edits turn the second guard branch of each gate into an always-true
# 'if-eq v1,v1,+4', so each 'return false' is unconditionally skipped
# (property-independent). Then: recompute DEX adler32 + SHA-1, and the STORED
# zip entry CRC32 in local header + central directory. OFFLINE. Verifies first.
import sys,struct,zlib,hashlib

APK='Bluetooth.apk'
OUT_APK='fable/Bluetooth.patched.apk'
OUT_DEX='fable/classes.patched.dex'

# dex-relative offsets (from dexdump) and expected originals
EDITS=[(0x179250, b'\x39\x01\x04\x00', b'\x32\x11\x04\x00'),  # 003e if-nez v1,0042 -> if-eq v1,v1,0042
       (0x179268, b'\x38\x01\x04\x00', b'\x32\x11\x04\x00')]  # 004a if-eqz v1,004e -> if-eq v1,v1,004e

def find_dex_region(apk):
    # locate local header for classes.dex
    import zipfile
    z=zipfile.ZipFile(apk); info=None
    for i in z.infolist():
        if i.filename=='classes.dex': info=i
    assert info is not None and info.compress_type==0, "classes.dex must be STORED"
    lho=info.header_offset
    sig,ver,flag,method,mt,md_,crc,csz,usz,nlen,elen=struct.unpack_from('<IHHHHHIIIHH',apk_bytes,lho)
    assert sig==0x04034b50, "bad local header sig"
    data_off=lho+30+nlen+elen
    return info,lho,data_off,info.file_size

def main():
    global apk_bytes
    apk_bytes=bytearray(open(APK,'rb').read())
    info,lho,dex_off,dex_size=find_dex_region(APK)
    print(f"classes.dex: local_hdr={lho} data_off={dex_off} size={dex_size} orig_crc={info.CRC:#010x}")
    # verify + apply dex edits (absolute offsets)
    for rel,orig,new in EDITS:
        a=dex_off+rel
        cur=bytes(apk_bytes[a:a+len(orig)])
        if cur!=orig:
            sys.exit(f"ABORT: dex@{rel:#x} = {cur.hex()}, expected {orig.hex()}")
        apk_bytes[a:a+len(new)]=new
        print(f"  patched dex@{rel:#x}: {orig.hex()} -> {new.hex()}")
    # recompute DEX internal checksum + signature over the dex bytes
    dex=apk_bytes[dex_off:dex_off+dex_size]
    assert dex[0:4]==b'dex\n', "not a dex"
    sha=hashlib.sha1(dex[32:]).digest()
    dex[12:32]=sha
    adler=zlib.adler32(dex[12:]) & 0xffffffff
    struct.pack_into('<I',dex,8,adler)
    apk_bytes[dex_off:dex_off+dex_size]=dex
    print(f"  dex new adler32={adler:#010x} sha1={sha.hex()}")
    # recompute zip CRC32 of the (now patched) STORED entry
    new_crc=zlib.crc32(bytes(dex)) & 0xffffffff
    # fix local header crc (lho+14)
    struct.pack_into('<I',apk_bytes,lho+14,new_crc)
    # fix central directory entry crc: scan central dir for this filename record
    # find End Of Central Directory to get CD offset
    eocd=apk_bytes.rfind(b'PK\x05\x06')
    assert eocd!=-1
    cd_off,=struct.unpack_from('<I',apk_bytes,eocd+16)
    cd_entries,=struct.unpack_from('<H',apk_bytes,eocd+10)
    p=cd_off; fixed=False
    for _ in range(cd_entries):
        assert apk_bytes[p:p+4]==b'PK\x01\x02'
        nlen,=struct.unpack_from('<H',apk_bytes,p+28)
        elen,=struct.unpack_from('<H',apk_bytes,p+30)
        clen,=struct.unpack_from('<H',apk_bytes,p+32)
        name=bytes(apk_bytes[p+46:p+46+nlen])
        if name==b'classes.dex':
            struct.pack_into('<I',apk_bytes,p+16,new_crc)  # crc field in central dir
            fixed=True
            print(f"  central-dir classes.dex crc -> {new_crc:#010x} at {p}")
        p+=46+nlen+elen+clen
    assert fixed, "classes.dex not found in central directory"
    open(OUT_APK,'wb').write(apk_bytes)
    open(OUT_DEX,'wb').write(bytes(dex))
    print("WROTE",OUT_APK,len(apk_bytes),"bytes ; and",OUT_DEX)
    # sanity: re-open patched apk with zipfile (verifies CRC on read)
    import zipfile
    z=zipfile.ZipFile(OUT_APK)
    d=z.read('classes.dex')
    print("re-read classes.dex OK, len",len(d),"crc matches:",(zlib.crc32(d)&0xffffffff)==new_crc)

main()
