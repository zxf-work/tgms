# ruff: noqa  (kept as run on xzgpu 2026-10-29; this header line is the only addition)
"""Run the committed storm CLI in-process with per-refresh phase logging.
usage: shrink_harness.py N batches workdir outdir store_src [mix]"""
import json, os, sys, time
N, B, work, out, src = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5]
mix = sys.argv[6] if len(sys.argv) > 6 else "c1"
WT = "/mnt/project/xzhang/tgms/work/shrink/wt"
sys.path.insert(0, WT); sys.path.insert(0, WT + "/scripts"); sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.makedirs(work, exist_ok=True); os.makedirs(out, exist_ok=True)
os.environ["TMPDIR"] = work
import tempfile; tempfile.tempdir = work
import shrink_instr as I
import bench_correction_storm as CLI
import tgms.eval.storm as S
I.install()
log = open(os.path.join(out, "calls.jsonl"), "w")
state = {"seg": 0, "batch": -1}
orig_refresh = S.refresh
orig_rb = S._registry_rollback
orig_rb_batch = S.Storm.run_batch

def refresh_w(record, handle, store, registry):
    I.reset()
    t = time.perf_counter()
    try:
        return orig_refresh(record, handle, store, registry)
    finally:
        tot = (time.perf_counter() - t) * 1000
        cur, cnt = I.snapshot()
        log.write(json.dumps({"b": state["batch"], "seg": state["seg"], "name": record.name,
                              "gen": record.generation, "total": tot, "ph": cur}) + "\n")
def rb_w(registry, mark):
    r = orig_rb(registry, mark); state["seg"] += 1; return r
def batch_w(self, bi):
    state["batch"] = bi; state["seg"] = 0
    return orig_rb_batch(self, bi)
S.refresh = refresh_w; S._registry_rollback = rb_w; S.Storm.run_batch = batch_w
t0 = time.time()
rc = CLI.main(["--store", src, "--n-artifacts", str(N), "--batches", str(B), "--seed", "0",
               "--mix", mix, "--measure-ttf", "end-to-end", "--check-cache", "chain",
               "--keep-work-dir", "--out", out])
log.close()
print("rc", rc, "wall_s", round(time.time() - t0, 1))
