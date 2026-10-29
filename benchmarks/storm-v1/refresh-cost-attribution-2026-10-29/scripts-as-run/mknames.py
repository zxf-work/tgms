# ruff: noqa  (kept as run on xzgpu 2026-10-29; this header line is the only addition)
"""Common-name sample across two stores. usage: mknames.py store1k store10k out.json"""
import json, os, random, sys
a, b, out = sys.argv[1:4]
def ops(d):
    r = {}
    for f in os.listdir(os.path.join(d, "ops")):
        r[f[:-5]] = open(os.path.join(d, "ops", f)).read()
    return r
A, B = ops(a), ops(b)
common = sorted(n for n in A if n in B and A[n] == B[n])
only_a = [n for n in A if n not in B]
kind = {n: json.loads(A[n])["op"] for n in common}
rng = random.Random(7)
s50 = rng.sample(common, 50)
strat = {}
for k in sorted(set(kind.values())):
    ns = [n for n in common if kind[n] == k]
    strat[k] = rng.sample(ns, min(20, len(ns)))
hist = lambda D: {k: sum(1 for v in D.values() if json.loads(v)["op"] == k) for k in sorted({json.loads(v)["op"] for v in D.values()})}
json.dump({"s50": s50, "strat": strat, "n_common": len(common), "only_in_a": only_a[:5], "n_only_a": len(only_a),
           "hist_a": hist(A), "hist_b": hist(B),
           "hist_b_first1000": hist({n: v for n, v in B.items() if int(n.split('-')[1]) < 1000})}, open(out, "w"), indent=1)
print(len(A), len(B), len(common), len(only_a))
