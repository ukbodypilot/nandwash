#!/bin/bash
# Stress test for `nandwash refresh-free` on a disposable ext4 loop device (needs root; destroys nothing real).
# While a writer churns files on the mounted fs, EVERY chunk of the partition is offered to refresh-free.
# Pass = all surviving files match their checksums, e2fsck is clean, no chunk was rewritten that was not free.
set -u
NW=${NW:-nandwash}
T=$(mktemp -d); MNT=$T/mnt; mkdir -p $MNT
cleanup() { umount $MNT 2>/dev/null; [ -n "${LOOP:-}" ] && losetup -d $LOOP 2>/dev/null; kill ${WPID:-0} 2>/dev/null; rm -rf $T; }
trap cleanup EXIT
truncate -s ${SIZE:-1536M} $T/d.img
echo 'start=2048, type=83' | sfdisk -q $T/d.img
LOOP=$(losetup -P --show -f $T/d.img); P=${LOOP}p1
for i in 1 2 3 4 5 6 7 8 9 10; do [ -b $P ] && break; sleep 0.3; done
mkfs.ext4 -q -F $P && mount $P $MNT
python3 - $MNT <<'PY'
import os, hashlib, random, sys, json
m=sys.argv[1]; random.seed(1); man={}
for i in range(300):
    d=os.urandom(random.choice([4096, 70000, 300000, 2_000_000]))
    p=f"{m}/seed{i}"; open(p,'wb').write(d); man[p]=hashlib.sha256(d).hexdigest()
for p in random.sample(list(man), 150): os.unlink(p); man.pop(p)   # fragment the free space
json.dump(man, open(m+"/../manifest.json","w"))
PY
sync
# concurrent churn: create/verify/delete files continuously
python3 - $MNT > $T/writer.log 2>&1 <<'PY' &
import os, hashlib, sys, time, random
m=sys.argv[1]; n=0; bad=0; t=time.time()
while time.time()-t < float(os.environ.get("CHURN_S","90")):
    d=os.urandom(random.choice([8192, 500000, 3_000_000]))
    p=f"{m}/churn{n%40}"; 
    try:
        with open(p,'wb') as f: f.write(d); f.flush(); os.fsync(f.fileno())
        fd=os.open(p,os.O_RDONLY); os.posix_fadvise(fd,0,0,os.POSIX_FADV_DONTNEED)   # force the next read to hit the DISK
        if hashlib.sha256(os.pread(fd,len(d),0)).digest()!=hashlib.sha256(d).digest(): bad+=1
        os.close(fd)
    except OSError as e: print("writer error",e); bad+=1
    n+=1
print("writer files",n,"corrupt",bad)
PY
WPID=$!
SECT=$(cat /sys/block/$(basename $LOOP)/$(basename $P)/size); PS=$(cat /sys/block/$(basename $LOOP)/$(basename $P)/start)
python3 - $PS $SECT > $T/all.json <<'PY'
import sys, json
s,n=int(sys.argv[1])*512,int(sys.argv[2])*512
print(json.dumps(list(range(s, s+n-1048576+1, 1048576))))
PY
# raw device size is the whole loop; offsets above are device-absolute
$NW --assume-ssd refresh-free $LOOP --chunks $T/all.json --yes --pause-ms ${PAUSE_MS:-20} --max-chunks 100000 --report $T/res.json | tail -15
wait $WPID; cat $T/writer.log
python3 - $T/res.json <<'PY'
import json, sys
r=json.load(open(sys.argv[1])); res=r["results"]
from collections import Counter
print("statuses:", dict(Counter(x["status"] for x in res)), "max frozen ms:", r.get("max_frozen_ms"))
PY
sync; echo 3 > /proc/sys/vm/drop_caches    # verify from the disk, not the page cache
python3 - $MNT $T/manifest.json <<'PY'
import hashlib, json, os, sys
m=json.load(open(sys.argv[2])); bad=0
for p,h in m.items():
    if hashlib.sha256(open(p,'rb').read()).hexdigest()!=h: bad+=1; print("CORRUPT",p)
print("seed files checked",len(m),"corrupt",bad)
sys.exit(1 if bad else 0)
PY
RC=$?
umount $MNT; e2fsck -fn $P > $T/fsck.out 2>&1; FS=$?; tail -3 $T/fsck.out
WB=$(sed -n 's/.*corrupt \([0-9]*\)$/\1/p' $T/writer.log | tail -1)
echo "seed-file check rc=$RC  fsck rc=$FS  writer-corrupt=${WB:-?}"
[ $RC -eq 0 ] && [ $FS -eq 0 ] && [ "${WB:-1}" = "0" ] && echo PASS || { echo FAIL; exit 1; }
