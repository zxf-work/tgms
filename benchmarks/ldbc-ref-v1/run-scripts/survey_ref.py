#!/usr/bin/env python3
import glob
import json
import os
import sys

d = sys.argv[1] if len(sys.argv) > 1 else "benchmarks/ldbc-ref-v1/ref-rows"
ok = err = 0
for f in sorted(glob.glob(os.path.join(d, "ref-*.json"))):
    rec = json.load(open(f))
    pid = os.path.basename(f)[4:-5]
    if "error" in rec:
        err += 1
        print("%-8s ERROR  %s" % (pid, rec["error"][:130]))
    else:
        ok += 1
        print("%-8s rows=%-7d wall_s=%s" % (pid, len(rec["rows"]), rec["wall_s"]))
print("\n%d ok, %d error" % (ok, err))
