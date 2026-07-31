"""§13: current-state versus bi-temporal overhead, across correction density.

The evaluation plan's §13 asks the first question a reviewer asks: *what do
two clocks cost when no historical question is being asked?* This script
answers it by building the same dataset twice from one event log (D-023) —

- **native-full** — the unmodified engine: replay, `compact()`, `gc()`.
  Closes live in segment sidecars; belief checks run as always.
- **native-current** — the §13 stripped configuration: an identical copy,
  then `compact_current_only()` + `gc()`. Superseded versions, close runs,
  and sidecars are physically dropped and the store is stamped
  ``CURRENT_ONLY``, after which it refuses past-belief queries and
  corrections rather than answering them wrongly.

Both stores answer the eleven current-belief registry queries; their hashes
must agree — that gate is the same command as the timing run, exactly as in
`eval_harness`. `hist.asof` runs on the full store only; on the stripped
store its refusal is recorded, not timed.

Reported per correction density (plan §13's five overheads):

- storage    — per-component bytes (segments, manifests, close runs, dict,
               event log) for both variants;
- load       — shared replay time, plus each variant's maintenance step
               (fold-compact+gc vs strip+gc). The write path is identical
               by construction, so load overhead *is* the maintenance delta;
- latency    — the current-belief registry under the §16.3 protocol;
- memory     — RSS after open and after one query pass, in a fresh
               subprocess per variant (`--probe` mode below);
- open time  — `tgms.open` plus time-to-first-query in that subprocess.

Density is swept (default 0, 0.01, 0.1, 1, 5, 20 percent of events) with the
correction mix held at the harness baseline's proportions (whole-interval :
carve : retract = 200 : 10 : 50), applied in batched write calls so a 20%
sweep does not pay one manifest per correction. The harness's own dataset is
untouched: its fixed ~261-op epoch 2 stays as the frozen-hash reference.
"""

from __future__ import annotations

import argparse
import json
import resource
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
# the probe subprocess runs bare `sys.executable`, so make the checkout
# importable regardless of the venv's install state (python-source = ".")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tgms  # noqa: E402
from tgms.core.model import EntityRef  # noqa: E402
from tgms.storage.base import make_op  # noqa: E402
from tgms.temporal.algebra import call_operator, ensure_all_registered  # noqa: E402

import eval_harness as H  # noqa: E402

#: Corrections per write batch. One batch per correction costs a full
#: manifest each (the 64 KB/correction finding of eval_writes); batching is
#: how a real backfill would apply them and keeps the sweep tractable.
CORRECTION_BATCH = 2000

#: The harness baseline's correction mix, as proportions of the total.
MIX_WHOLE, MIX_CARVE, MIX_RETRACT = 200 / 260, 10 / 260, 50 / 260


def _ref_json(ref: EntityRef) -> dict[str, Any]:
    if ref.kind == "node":
        return {"kind": "node", "uid": ref.uid}
    return {"kind": "edge", "src": ref.src, "dst": ref.dst,
            "rel_type": ref.rel_type, "disc": ref.disc}


def _batched(store: tgms.Store, ops: list[dict[str, Any]]) -> None:
    """Apply ops through the write-ahead path in batches.

    Uses `Store._write` deliberately: the ops must go through the event log
    (D-023 — every store replays the same log) and one batch per op would
    measure manifest churn, not correction density.
    """
    for i in range(0, len(ops), CORRECTION_BATCH):
        store._write(ops[i:i + CORRECTION_BATCH])


def build_log(scale: int, corr_pct: float) -> tuple[H.Dataset, dict[str, int]]:
    """Write the reference event log at one correction density.

    Epoch 1 is `eval_harness`'s generator verbatim — same events, same
    identities. Epoch 2 scales the harness's correction mix to
    `corr_pct` percent of `scale`, uniformly spread (the plan's age
    profiles are §12.3's axis, not §13's).
    """
    path = Path(tempfile.mkdtemp(prefix="tgms-bitemporal-")) / "reference"
    store = tgms.open(path, backend="native")
    store.ingest_events([H._event(i, scale) for i in range(scale)])
    tt_epoch1 = store.assert_node("n1", "Node", {"name": "alpha"},
                                  vt_s=0, vt_e=scale)

    total = round(scale * corr_pct / 100.0)
    counts = {"whole": 0, "carve": 0, "retract": 0, "node": 0, "total": 0}
    if total > 0:
        n_whole = max(1, round(total * MIX_WHOLE))
        n_carve = round(total * MIX_CARVE)
        n_retract = round(total * MIX_RETRACT)

        ops: list[dict[str, Any]] = []
        step = max(1, scale // n_whole)
        for i in range(0, scale, step):
            e = H._event(i, scale)
            ops.append(make_op("correct", ref=_ref_json(H._edge_ref(i, scale)),
                               props={"weight": 2}, vt_s=e["vt_s"], vt_e=e["vt_e"],
                               source="ingest", provenance_ref=None))
        counts["whole"] = len(ops)
        _batched(store, ops)

        store.correct(EntityRef(kind="node", uid="n1"),
                      {"name": "alpha-corrected"}, vt_s=0, vt_e=scale)
        counts["node"] = 1

        # partial corrections: rewrite the tail half of an interval, which
        # splits it — the harness keeps ~10 of these so diff_snapshots's
        # props_changed branch is exercised; here they scale with density
        life, mid = H.edge_life(scale), scale // 2
        band = life // 2
        ops = []
        if n_carve > 0:
            stride = max(1, band // n_carve)
            for i in range(mid - band, mid, stride):
                e = H._event(i, scale)
                ops.append(make_op("correct", ref=_ref_json(H._edge_ref(i, scale)),
                                   props={"weight": 3},
                                   vt_s=e["vt_s"] + band, vt_e=e["vt_e"],
                                   source="ingest", provenance_ref=None))
        counts["carve"] = len(ops)
        _batched(store, ops)

        ops = []
        if n_retract > 0:
            step_r = max(1, scale // n_retract)
            for i in range(0, scale, step_r):
                ops.append(make_op("retract", ref=_ref_json(H._edge_ref(i, scale)),
                                   t=H._event(i, scale)["vt_s"] + life // 2,
                                   source="ingest", provenance_ref=None))
        counts["retract"] = len(ops)
        _batched(store, ops)

    counts["total"] = counts["whole"] + counts["carve"] + counts["retract"] + counts["node"]
    store.close()
    data = H.Dataset(path / "eventlog.jsonl", scale, tt_epoch1, t0=0, t1=scale,
                     filter_uids=tuple(f"n{i}" for i in range(H.MOTIF_FILTER)),
                     name=f"synth-corr{corr_pct:g}pct")
    return data, counts


# --- storage accounting --------------------------------------------------- #

def storage_breakdown(store_path: Path) -> dict[str, int]:
    """One accounting, bucketed by component. Bytes, recursive, every file."""
    native = store_path / "native"
    buckets = {
        "segments": native / "seg",
        "manifests": native / "manifests",
        "close_runs": native / "close",
    }
    out: dict[str, int] = {}
    for name, root in buckets.items():
        out[name] = sum(f.stat().st_size for f in root.rglob("*") if f.is_file()) \
            if root.exists() else 0
    out["dict"] = (native / "dict.log").stat().st_size if (native / "dict.log").exists() else 0
    accounted = set(buckets.values())
    other = 0
    for f in native.rglob("*"):
        if f.is_file() and f.name != "dict.log" and not any(
                p in accounted for p in f.parents):
            other += f.stat().st_size
    out["other"] = other  # CURRENT, CURRENT_ONLY, idx/
    out["store_total"] = sum(out.values())
    log = store_path / "eventlog.jsonl"
    out["eventlog"] = log.stat().st_size if log.exists() else 0
    out["total_with_log"] = out["store_total"] + out["eventlog"]
    return out


# --- query measurement ---------------------------------------------------- #

def run_queries(store_path: Path, queries: list[H.Query]) -> list[H.Result]:
    """`eval_harness.run_system`, but on a store that already exists."""
    ensure_all_registered()
    store = tgms.open(store_path, backend="native")
    adapter = store.adapter
    out: list[H.Result] = []
    for q in queries:
        try:
            payload, timings = H._measure(
                lambda: call_operator(adapter, q.op, dict(q.args)))
            p50, p95 = H._p50_p95(timings)
            out.append(H.Result(q.id, True, H.canonical_hash(payload), p50, p95,
                                int(H._answer_size(payload)),
                                timings_ms=[round(t, 3) for t in timings]))
        except Exception as e:
            out.append(H.Result(q.id, False,
                                error=f"{type(e).__name__}: {e}"[:160]))
    store.close()
    return out


# --- subprocess probe: open time and memory ------------------------------- #

def _rss_kb() -> dict[str, int]:
    out = {}
    status = Path("/proc/self/status")
    if status.exists():  # Linux (the measurement host)
        for line in status.read_text().splitlines():
            if line.startswith(("VmRSS:", "VmHWM:")):
                k, v = line.split(":")
                out[k.lower()] = int(v.split()[0])
    ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # ru_maxrss is KB on Linux, bytes on macOS
    out["maxrss_kb"] = ru // 1024 if sys.platform == "darwin" else ru
    return out


def probe(store_path: Path, params: dict[str, Any]) -> dict[str, Any]:
    """Fresh-process measurement: open, first query, one pass of the suite.

    In-process caches (segment cache, postings, TCSR) start empty here, so
    open and first-query times mean something; the OS page cache is warm
    from the build, which the record states rather than hides.
    """
    ensure_all_registered()
    t0 = time.perf_counter()
    store = tgms.open(store_path, backend="native")
    open_ms = (time.perf_counter() - t0) * 1e3
    after_open = _rss_kb()

    queries = [q for q in H.registry(params["t0"], params["t1"],
                                     params["tt_epoch1"],
                                     tuple(params["filter_uids"]))
               if "bitemporal" not in q.requires]
    t0 = time.perf_counter()
    call_operator(store.adapter, queries[0].op, dict(queries[0].args))
    first_ms = (time.perf_counter() - t0) * 1e3

    for q in queries:
        try:
            call_operator(store.adapter, q.op, dict(q.args))
        except Exception:
            pass  # refusals are recorded by the parent, not here
    after_suite = _rss_kb()
    store.close()
    return {"open_ms": round(open_ms, 3), "first_query_ms": round(first_ms, 3),
            "rss_after_open_kb": after_open, "rss_after_suite_kb": after_suite,
            "cache_state": "process-cold, page-cache-warm"}


def probe_subprocess(store_path: Path, data: H.Dataset) -> dict[str, Any]:
    params = {"t0": data.t0, "t1": data.t1, "tt_epoch1": data.tt_epoch1,
              "filter_uids": list(data.filter_uids)}
    r = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--probe",
         str(store_path), "--probe-params", json.dumps(params)],
        capture_output=True, text=True, check=True, cwd=H.ROOT)
    return json.loads(r.stdout.strip().splitlines()[-1])


# --- the sweep ------------------------------------------------------------ #

def run_density(scale: int, pct: float, keep: bool) -> dict[str, Any]:
    rec: dict[str, Any] = {"pct": pct}
    t0 = time.perf_counter()
    data, counts = build_log(scale, pct)
    rec["corrections"] = counts
    rec["build_s"] = round(time.perf_counter() - t0, 1)

    work = Path(tempfile.mkdtemp(prefix="tgms-bitemporal-stores-"))
    full, curr = work / "full", work / "current"

    t0 = time.perf_counter()
    H.load_store(full, "native", data.log)
    rec["replay_s"] = round(time.perf_counter() - t0, 1)
    rec["storage_replayed"] = storage_breakdown(full)

    # the stripped store starts as a byte-identical copy of the replayed one
    shutil.copytree(full, curr)

    # keep_last=0: the storage comparison is between steady-state stores, so
    # neither variant may carry retention headroom — with the default (2) the
    # stripped store still holds its pre-strip segments and close runs on
    # disk and the comparison measures gc policy, not bi-temporality
    t0 = time.perf_counter()
    s = tgms.open(full, backend="native")
    s.adapter.compact()
    s.adapter.gc(keep_last=0)
    s.close()
    rec["maintain_s"] = round(time.perf_counter() - t0, 1)

    t0 = time.perf_counter()
    s = tgms.open(curr, backend="native")
    strip_report = s.adapter.compact_current_only()
    s.adapter.gc(keep_last=0)
    s.close()
    rec["strip_s"] = round(time.perf_counter() - t0, 1)
    rec["gc_keep_last"] = 0
    # replayed stores hold only an event-log header; the log of record is
    # the reference log every variant replays (D-023)
    rec["reference_log_bytes"] = data.log.stat().st_size
    rec["strip_report"] = {k: int(v) for k, v in strip_report.items()}

    queries = H.registry(data.t0, data.t1, data.tt_epoch1, data.filter_uids)
    current_queries = [q for q in queries if "bitemporal" not in q.requires]
    bitemporal_queries = [q for q in queries if "bitemporal" in q.requires]

    rec["full"] = {
        "storage": storage_breakdown(full),
        "results": [vars(r) for r in run_queries(full, queries)],
        "probe": probe_subprocess(full, data),
    }
    curr_results = run_queries(curr, current_queries)
    refused = run_queries(curr, bitemporal_queries)
    rec["current_only"] = {
        "storage": storage_breakdown(curr),
        "results": [vars(r) for r in curr_results],
        "refused": {r.query: r.error for r in refused},
        "probe": probe_subprocess(curr, data),
    }
    for r in refused:
        if r.ok:
            raise SystemExit(f"GATE: current-only store answered {r.query} "
                             f"instead of refusing it")

    full_by_id = {r["query"]: r for r in rec["full"]["results"]}
    mismatches = [r.query for r in curr_results
                  if not r.ok or r.hash != full_by_id[r.query]["hash"]]
    rec["agree"] = not mismatches
    rec["mismatches"] = mismatches

    if keep:
        rec["stores"] = str(work)
    else:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(data.log.parent.parent, ignore_errors=True)
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", type=int, default=1_000_000)
    ap.add_argument("--densities", default="0,0.01,0.1,1,5,20",
                    help="correction densities, percent of events")
    ap.add_argument("--json", type=Path)
    ap.add_argument("--keep", action="store_true",
                    help="keep the temp stores for inspection")
    ap.add_argument("--probe", type=Path, help="internal: probe one store")
    ap.add_argument("--probe-params", help="internal: dataset params as JSON")
    args = ap.parse_args()

    if args.probe:
        print(json.dumps(probe(args.probe, json.loads(args.probe_params))))
        return 0

    densities = [float(d) for d in args.densities.split(",") if d.strip() != ""]
    dummy = H.Dataset(Path("unused"), args.scale, 0, 0, args.scale,
                      name=f"synth-bitemporal-{args.scale}")
    meta = H.manifest(dummy, ["native-full", "native-current"])
    meta["densities_pct"] = densities
    meta["correction_batch"] = CORRECTION_BATCH

    print(f"§13 bi-temporal overhead — scale {args.scale:,}, "
          f"densities {densities} (pct)")
    print(f"  commit {meta['commit']}{' (dirty)' if meta['dirty'] else ''}")
    if not meta["on_measurement_host"]:
        print(f"  WARNING: not {H.MEASUREMENT_HOST} — development run, "
              f"timings are not comparable with reported numbers")

    records = []
    for pct in densities:
        print(f"\n== density {pct:g}% ==", flush=True)
        rec = run_density(args.scale, pct, args.keep)
        records.append(rec)
        f_lat = {r['query']: r['p50_ms'] for r in rec['full']['results'] if r['ok']}
        c_lat = {r['query']: r['p50_ms'] for r in rec['current_only']['results'] if r['ok']}
        print(f"  corrections {rec['corrections']['total']:,} | "
              f"replay {rec['replay_s']}s | maintain {rec['maintain_s']}s | "
              f"strip {rec['strip_s']}s | agree {rec['agree']}")
        print(f"  store bytes: full {rec['full']['storage']['store_total']:,} "
              f"vs current {rec['current_only']['storage']['store_total']:,}")
        for qid in sorted(c_lat):
            print(f"    {qid:<18} full {f_lat.get(qid, float('nan')):>9.2f} ms"
                  f"  current {c_lat[qid]:>9.2f} ms")
        if not rec["agree"]:
            print(f"  HASH MISMATCH: {rec['mismatches']}")

    if args.json:
        args.json.write_text(json.dumps(
            {"manifest": meta, "densities": records,
             "agree": all(r["agree"] for r in records)},
            indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"\nwrote {args.json}")

    return 0 if all(r["agree"] for r in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
