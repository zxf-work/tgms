# ruff: noqa  (kept as run on xzgpu 2026-10-29; this header line is the only addition)
"""Separate-process profile of refresh() on a registered store.
usage: shrink_profile.py storedir out_prefix names_json"""
import cProfile, io, json, os, pstats, random, statistics as st, sys, time
store_dir, outp, names_json = sys.argv[1], sys.argv[2], sys.argv[3]
WT = "/mnt/project/xzhang/tgms/work/shrink/wt"
sys.path.insert(0, WT); sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shrink_instr as I
import tgms
from tgms.artifact.refresh import refresh
from tgms.artifact.registry import Registry
from tgms.artifact.witness import RefreshHandle
from pathlib import Path
I.install()
sel = json.load(open(names_json))  # {"s50": [...], "strat": {kind: [names]}}
sd = Path(store_dir)

t = time.perf_counter(); registry = Registry(sd); load_ms = (time.perf_counter() - t) * 1000
store = tgms.open(sd, backend="native")
info = {"store_dir": store_dir, "registry_load_ms": load_ms,
        "registry_bytes_before": registry.path.stat().st_size,
        "n_names": len(registry._by_name),
        "n_entries": sum(len(h) for h in registry._by_name.values()),
        "gens_per_name_max": max(len(h) for h in registry._by_name.values()),
        "gens_per_name_mean": sum(len(h) for h in registry._by_name.values()) / len(registry._by_name),
        "eventlog_bytes": store.eventlog.size(), "results_files": len(os.listdir(sd / "results")) if (sd / "results").exists() else 0}

def op_of(name):
    return json.load(open(sd / "ops" / (name + ".json")))["op"]

def one(name):
    rec = registry.current(name)
    h = RefreshHandle(rec.id, rec.refresh["kind"], rec.refresh["ref"], rec.plan.get("plan_format"),
                      rec.refresh.get("basis_policy", "open"))
    I.reset()
    t = time.perf_counter()
    refresh(rec, h, store, registry)
    tot = (time.perf_counter() - t) * 1000
    cur, _ = I.snapshot()
    return tot, cur

# warm-up, discarded
for n in sel["s50"][:10]:
    one(n)

def run(names, tag):
    rows = []
    for n in names:
        tot, cur = one(n)
        rows.append({"name": n, "op": op_of(n), "total": tot, "ph": cur, "tag": tag})
    return rows

allrows = []
for r in range(3):
    allrows += run(sel["s50"], "s50.r%d" % r)
strat = []
for kind, names in sorted(sel["strat"].items()):
    strat += run(names, "strat")
allrows += strat

# cProfile pass over the same 50 names
pr = cProfile.Profile()
pr.enable()
for n in sel["s50"]:
    one(n)
pr.disable()
s = io.StringIO(); ps = pstats.Stats(pr, stream=s).sort_stats("tottime"); ps.print_stats(30)
open(outp + ".cprofile_tottime.txt", "w").write(s.getvalue())
s = io.StringIO(); ps = pstats.Stats(pr, stream=s).sort_stats("cumulative"); ps.print_stats(45)
open(outp + ".cprofile_cum.txt", "w").write(s.getvalue())
def fn_key(sub):
    out = {}
    for (f, ln, nm), (cc, nc, tt, ct, _c) in ps.stats.items():
        if sub in "%s:%d(%s)" % (f, ln, nm):
            out["%s:%d(%s)" % (os.path.basename(f), ln, nm)] = {"calls": nc, "tt_ms": tt * 1000, "ct_ms": ct * 1000}
    return out
prof_sel = {}
for k in ["_run_operator", "_publish", "register", "_append_locked", "append", "fsync", "flock", "canonical_json",
          "extend_chain", "result_put", "put", "digest", "from_json", "_reload_tail_locked", "_verify_append_landed",
          "_append_to_index", "to_json", "evaluate_leaf", "_canonicalize_floats", "dumps", "write_text", "sha256"]:
    prof_sel.update(fn_key(k))
info["registry_bytes_after"] = registry.path.stat().st_size
info["n_entries_after"] = sum(len(h) for h in registry._by_name.values())
json.dump({"info": info, "rows": allrows, "cprofile_sel": prof_sel, "cprofile_total_s": ps.total_tt},
          open(outp + ".json", "w"))
print(json.dumps(info))
