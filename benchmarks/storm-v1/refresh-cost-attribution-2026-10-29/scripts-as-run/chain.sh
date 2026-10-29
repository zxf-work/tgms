#!/bin/bash
cd /mnt/project/xzhang/tgms/work/shrink
PY=/mnt/project/xzhang/tgms/venv/bin/python
echo "chain start $(date -u) load $(cut -d" " -f1-3 /proc/loadavg)"
for N in 1000 10000; do
  echo "harness N=$N start $(date -u) load $(cut -d" " -f1-3 /proc/loadavg)"
  nice -n 19 $PY shrink_harness.py $N 2 run$N/w run$N/o store-src c1 > run$N.harness.log 2>&1
  echo "harness N=$N done $(date -u) rc=$? load $(cut -d" " -f1-3 /proc/loadavg)"
done
S1=$(ls -d run1000/w/storm-*/store); S10=$(ls -d run10000/w/storm-*/store)
$PY mknames.py $S1 $S10 names.json
for N in 1000 10000; do
  S=$(ls -d run$N/w/storm-*/store)
  echo "profile N=$N start $(date -u) load $(cut -d" " -f1-3 /proc/loadavg)"
  nice -n 19 $PY shrink_profile.py $S prof$N names.json > prof$N.log 2>&1
  echo "profile N=$N done $(date -u) load $(cut -d" " -f1-3 /proc/loadavg)"
done
echo "chain done $(date -u)"
