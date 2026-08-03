"""Concurrency evaluation: mixed writer+readers, commit cost, residency.

D-043 item 3. Every concurrent number this project had was readers-only
(§14.4 of `docs/eval_resources.md`): N readers over a *quiescent* store.
This measures the three things that were missing.

* **mixed** — what a live writer costs concurrent readers, and what
  concurrent readers cost the writer. Reported as *distributions*: reader
  per-query p50/p90/p99 and writer commit p50/p90/p99, over several trials,
  because concurrency is the noisiest thing this project measures and a
  single median from a single trial would be a story rather than a result.
  Aggregate throughput and per-query latency are reported side by side and
  never substituted for one another.

* **commitcost** — where a singleton commit's milliseconds actually go, by
  engine phase, as the store's history grows. `engine_lessons.md` §7 blames
  "several fsyncs"; `docs/eval_writes.md` blames "a fresh full manifest per
  commit naming every segment". Those are different fixes. Measure first.

* **groupcommit** — what coalescing queued single writes into one durable
  generation buys, on the write patterns that exist.

* **residency** — the D-045 index-budget decision: what a resident TCSR
  costs a mixed read workload, against what dropping it costs to rebuild.

Every mode runs one fresh process per condition (§9g: queries measured in
one process are not independent), and readers open `read_only=True` because
a read-write handle performs crash recovery and is therefore a second
writer.

    uv run python scripts/eval_concurrency.py mixed --scale 1000000 \
        --readers 1,2,4,8 --trials 3 --json out.json
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import eval_harness as H  # noqa: E402
import eval_resources as R  # noqa: E402

#: The read mix is §14.4's, taken from the same registry: a postings point
#: lookup, a traversal that builds the TCSR, and two full-window scans. Same
#: queries, same dataset, so the readers-only table is the baseline this
#: compares against rather than a differently-shaped neighbour.
READER_MIX = R.READER_MIX

#: The path query the residency decision turns on, and the scan it taxes.
PATH_QUERY = "paths.k"
SCAN_QUERY = "series.count"


def pctl(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))]


def dist(xs: list[float]) -> dict[str, Any]:
    """A distribution, never a single median. Concurrency measurements are
    the noisiest thing here; one number would hide the shape that matters."""
    if not xs:
        return {"n": 0}
    return {"n": len(xs),
            "p50": round(statistics.median(xs), 3),
            "p90": round(pctl(xs, 0.90), 3),
            "p99": round(pctl(xs, 0.99), 3),
            "min": round(min(xs), 3), "max": round(max(xs), 3)}


def merge_dists(runs: list[list[float]]) -> dict[str, Any]:
    """Per-trial p50s kept separately, plus the pooled distribution: a
    difference smaller than the spread across trials is a tie, not a win."""
    per_trial = [round(statistics.median(r), 3) for r in runs if r]
    pooled = [x for r in runs for x in r]
    return {"trials": len(per_trial), "trial_p50s": per_trial,
            "pooled": dist(pooled)}


# --------------------------------------------------------------------------- #
# child processes                                                             #
# --------------------------------------------------------------------------- #


def _spawn(mode: str, cfg: dict[str, Any]) -> subprocess.Popen:
    p = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "_child", mode],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=dict(os.environ))
    assert p.stdin is not None
    p.stdin.write(json.dumps(cfg))
    p.stdin.close()
    p.stdin = None
    return p


def _collect(p: subprocess.Popen, timeout_s: float) -> dict[str, Any]:
    try:
        out, err = p.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        p.kill()
        return {"failed": "timeout"}
    if p.returncode != 0:
        return {"failed": f"rc={p.returncode}", "stderr": err[-800:]}
    try:
        return json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"failed": "unparseable", "stdout": out[-800:], "stderr": err[-800:]}


def child_reader(cfg: dict[str, Any]) -> dict[str, Any]:
    """Loop the read mix inside a barrier-aligned wall-clock window."""
    import tgms
    from tgms.temporal.algebra import call_operator, ensure_all_registered

    ensure_all_registered()
    store = tgms.open(Path(cfg["store"]), backend="native", read_only=True)
    a = store.adapter
    mix = cfg["mix"]

    for q in mix:                       # one warm pass, per process
        try:
            call_operator(a, q["op"], dict(q["args"]))
        except Exception:               # noqa: BLE001 — a refusal is data
            pass
    while time.time() < cfg["start_at"]:
        time.sleep(0.005)

    timings: dict[str, list[float]] = {q["id"]: [] for q in mix}
    errors: dict[str, str] = {}
    t_begin = time.perf_counter()
    deadline = t_begin + cfg["duration_s"]
    done = 0
    while time.perf_counter() < deadline:
        for q in mix:
            try:
                t = time.perf_counter()
                call_operator(a, q["op"], dict(q["args"]))
                timings[q["id"]].append((time.perf_counter() - t) * 1e3)
                done += 1
            except Exception as e:      # noqa: BLE001
                errors[q["id"]] = f"{type(e).__name__}: {e}"[:160]
    wall = time.perf_counter() - t_begin
    store.close()
    return {"role": "reader", "generation": a.generation,
            "wall_s": round(wall, 3), "queries_done": done,
            "qps": round(done / wall, 3),
            "per_query": {k: dist(v) for k, v in timings.items()},
            "raw": {k: [round(x, 3) for x in v[:4000]] for k, v in timings.items()},
            "errors": errors}


def child_writer(cfg: dict[str, Any]) -> dict[str, Any]:
    """Commit batches for a wall-clock window, timing every commit."""
    import tgms

    store = tgms.open(Path(cfg["store"]), backend="native")
    rows = int(cfg["batch_rows"])
    base = int(cfg["vt_base"])
    while time.time() < cfg["start_at"]:
        time.sleep(0.005)

    lat: list[float] = []
    phases: list[dict[str, int]] = []
    t_begin = time.perf_counter()
    deadline = t_begin + cfg["duration_s"]
    n = 0
    while time.perf_counter() < deadline:
        events = [{"src": f"cw{n + j}", "dst": f"cx{n + j}", "rel_type": "W",
                   "vt_s": base + n + j} for j in range(rows)]
        t = time.perf_counter()
        store.ingest_events(events)
        lat.append((time.perf_counter() - t) * 1e3)
        ph = store.adapter._store.last_commit_phases()
        if ph is not None:
            phases.append({k: int(v) for k, v in ph.items()})
        n += rows
    wall = time.perf_counter() - t_begin
    gen = store.adapter.generation
    store.close()
    return {"role": "writer", "commits": len(lat), "rows_written": n,
            "wall_s": round(wall, 3), "generation": gen,
            "commit_ms": dist(lat), "raw": [round(x, 3) for x in lat[:4000]],
            "commits_per_s": round(len(lat) / wall, 3),
            "phase_p50_us": {k: int(statistics.median([p[k] for p in phases]))
                             for k in phases[0]} if phases else None}


# --------------------------------------------------------------------------- #
# mode: mixed (writer + N readers)                                            #
# --------------------------------------------------------------------------- #


@contextlib.contextmanager
def _pristine(store: Path):
    """A private copy of `store`, removed afterwards."""
    import shutil
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="tgms-mixed-"))
    live = tmp / store.name
    shutil.copytree(store, live)
    try:
        yield live
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def cmd_mixed(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    data, d = R.ensure_dataset(Path(args.workdir), args.scale)
    store = Path(args.store) if args.store else R.ensure_store(d, "native", data.log)
    mix = R.queries_for(data, READER_MIX)
    counts = [int(x) for x in args.readers.split(",")]
    duration = args.duration
    lead = args.lead

    records: list[dict[str, Any]] = []
    for n in counts:
        for with_writer in (False, True):
            reader_runs: dict[str, list[list[float]]] = {q["id"]: [] for q in mix}
            commit_runs: list[list[float]] = []
            trial_rows: list[dict[str, Any]] = []
            for trial in range(args.trials):
                # Every trial gets its own copy. The writer grows the store by
                # thousands of generations, so a shared one would make the
                # later conditions measure a different store than the earlier
                # ones — and would quietly corrupt the cached §14.4 store that
                # every other harness replays from.
                with _pristine(store) as live:
                    start_at = time.time() + lead
                    rcfg = {"store": str(live), "start_at": start_at,
                            "duration_s": duration, "mix": mix}
                    wcfg = {"store": str(live), "start_at": start_at,
                            "duration_s": duration,
                            "batch_rows": args.batch_rows,
                            "vt_base": 10_000_000 + trial * 1_000_000}
                    procs = [_spawn("reader", rcfg) for _ in range(n)]
                    wproc = _spawn("writer", wcfg) if with_writer else None
                    readers = [_collect(p, duration + lead + 900) for p in procs]
                    writer = (_collect(wproc, duration + lead + 900)
                              if wproc else None)

                ok = [r for r in readers if not r.get("failed")]
                if len(ok) != n or (wproc and writer.get("failed")):
                    trial_rows.append({"trial": trial, "failed": True,
                                       "readers": readers, "writer": writer})
                    continue
                for q in mix:
                    pooled = [x for r in ok for x in r["raw"].get(q["id"], [])]
                    reader_runs[q["id"]].append(pooled)
                if writer:
                    commit_runs.append(writer["raw"])
                trial_rows.append({
                    "trial": trial,
                    "aggregate_qps": round(sum(r["qps"] for r in ok), 2),
                    "reader_generations": sorted({r["generation"] for r in ok}),
                    "writer": None if not writer else {
                        "commits": writer["commits"],
                        "commit_ms": writer["commit_ms"],
                        "commits_per_s": writer["commits_per_s"],
                        "phase_p50_us": writer["phase_p50_us"]}})

            good = [t for t in trial_rows if not t.get("failed")]
            records.append({
                "readers": n, "writer": with_writer,
                "aggregate_qps": [t["aggregate_qps"] for t in good],
                "per_query_ms": {q["id"]: merge_dists(reader_runs[q["id"]])
                                 for q in mix},
                "commit_ms": merge_dists(commit_runs) if commit_runs else None,
                "trials": trial_rows})
            label = "writer" if with_writer else "idle  "
            qps = [t["aggregate_qps"] for t in good]
            print(f"  n={n:<3} {label} agg {qps} q/s | "
                  + " ".join(
                      f"{q['id']}={records[-1]['per_query_ms'][q['id']]['trial_p50s']}"
                      for q in mix), flush=True)
    return 0, {"mode": "mixed", "store": str(store), "scale": args.scale,
               "reader_counts": counts, "queries": [q["id"] for q in mix],
               "duration_s": duration, "trials": args.trials,
               "batch_rows": args.batch_rows, "records": records}


# --------------------------------------------------------------------------- #
# mode: commitcost — where a singleton commit's time goes                     #
# --------------------------------------------------------------------------- #


def _timed_write(store, events: list[dict[str, Any]]) -> dict[str, int]:
    """One write batch, driven layer by layer instead of through `_write`.

    Deliberately duplicates `Store._write` so the three layers can be timed
    separately: the write-ahead log fsync, the Python bi-temporal semantics
    (`apply_ops`), and the engine commit — which reports its own phase split.
    Kept honest by asserting the generation advanced by exactly one.
    """
    from tgms.storage.base import make_op

    a = store.adapter
    gen0 = a.generation
    ops = [make_op("ingest_events", events=events, offset=0, node_label="Node",
                   source="ingest", provenance_ref=None)]
    t_all = time.perf_counter()
    tt = store.clock.tick()
    t = time.perf_counter()
    _bid, end_offset, record = store.eventlog.append(tt, ops)
    wal_us = int((time.perf_counter() - t) * 1e6)
    store._chain = __import__("tgms.storage.eventlog", fromlist=["x"]).extend_chain(
        store._chain if store._chain is not None
        else store.eventlog.chain_of_prefix(end_offset - len(record)), record)
    t = time.perf_counter()
    a.begin()
    a.apply_ops(ops, tt)
    apply_us = int((time.perf_counter() - t) * 1e6)
    a.note_event_cursor(end_offset, store._chain)
    a.commit()
    write_us = int((time.perf_counter() - t_all) * 1e6)
    assert a.generation == gen0 + 1, "a batch must publish exactly one generation"
    ph = {k: int(v) for k, v in a._store.last_commit_phases().items()}
    ph.update({"wal_us": wal_us, "apply_us": apply_us, "write_us": write_us})
    return ph


def cmd_commitcost(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    """Commit a singleton batch repeatedly and watch the phases move.

    The point is the *shape over generations*: fsync cost is flat, manifest
    cost grows with the number of segments the manifest has to name. Whichever
    grows is the one group-commit has to beat.
    """
    import tempfile

    import tgms

    rows = [int(x) for x in args.batch_sizes.split(",")]
    out: list[dict[str, Any]] = []
    for batch in rows:
        root = Path(tempfile.mkdtemp(prefix="tgms-cc-")) / "s"
        s = tgms.open(root, backend="native")
        s.ingest_events([{"src": f"p{i}", "dst": f"q{i}", "rel_type": "R",
                          "vt_s": i} for i in range(args.seed_rows)])
        lat, phases = [], []
        n = 0
        for _ in range(args.commits):
            events = [{"src": f"a{n + j}", "dst": f"b{n + j}", "rel_type": "R",
                       "vt_s": 1_000_000 + n + j} for j in range(batch)]
            phases.append(_timed_write(s, events))
            lat.append(phases[-1]["write_us"] / 1e3)
            n += batch
        # first vs last decile: does the cost grow with store history?
        k = max(1, len(phases) // 10)

        def med(ps: list[dict], key: str) -> int:
            return int(statistics.median([p[key] for p in ps]))

        keys = sorted(phases[0])
        row = {
            "batch_rows": batch, "commits": args.commits,
            "commit_ms": dist(lat),
            "ms_per_row": round(statistics.median(lat) / batch, 4),
            "phase_p50_us": {k2: med(phases, k2) for k2 in keys},
            "first_decile_us": {k2: med(phases[:k], k2) for k2 in keys},
            "last_decile_us": {k2: med(phases[-k:], k2) for k2 in keys},
            "store_bytes": sum(f.stat().st_size
                               for f in root.rglob("*") if f.is_file()),
        }
        out.append(row)
        p = row["phase_p50_us"]
        first, last = row["first_decile_us"], row["last_decile_us"]
        print(f"  batch={batch:<5} write p50 {row['commit_ms']['p50']:8.2f} ms "
              f"({row['ms_per_row']:.4f} ms/row)", flush=True)
        print(f"      wal={p['wal_us'] / 1e3:.2f} apply={p['apply_us'] / 1e3:.2f} "
              f"| engine {p['total_us'] / 1e3:.2f} = seal {p['seal_us'] / 1e3:.2f} "
              f"+ dict {p['dict_us'] / 1e3:.2f} + manifest "
              f"{p['manifest_us'] / 1e3:.2f} + CURRENT {p['current_us'] / 1e3:.2f}",
              flush=True)
        print(f"      manifest {p['manifest_bytes']:,} B naming "
              f"{p['segments_named']} segs | growth first->last decile: "
              f"write {first['write_us'] / 1e3:.2f}->"
              f"{last['write_us'] / 1e3:.2f} ms, "
              f"manifest {first['manifest_bytes']:,}->{last['manifest_bytes']:,} B",
              flush=True)
        s.close()
    return 0, {"mode": "commitcost", "seed_rows": args.seed_rows,
               "commits": args.commits, "records": out}


# --------------------------------------------------------------------------- #
# mode: groupcommit                                                           #
# --------------------------------------------------------------------------- #


def cmd_groupcommit(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    """Concurrent single-row writers, with and without coalescing.

    The workload group-commit exists for: N callers each holding one row,
    none of which can batch on its own. Without coalescing each is a durable
    generation; with it, whatever is queued when the committer wakes shares
    one.
    """
    import tempfile

    import tgms
    from tgms.write import GroupCommitWriter

    out = []
    for writers in [int(x) for x in args.writers.split(",")]:
        for coalesce in (False, True):
            root = Path(tempfile.mkdtemp(prefix="tgms-gc-")) / "s"
            s = tgms.open(root, backend="native")
            s.ingest_events([{"src": f"p{i}", "dst": f"q{i}", "rel_type": "R",
                              "vt_s": i} for i in range(args.seed_rows)])
            gc = GroupCommitWriter(s, max_delay_s=args.max_delay,
                                   max_batch=args.max_batch) if coalesce else None
            if gc:
                gc.start()
            gen0 = s.adapter.generation
            t0 = time.perf_counter()
            lat = _drive_writers(s, gc, writers, args.rows_per_writer)
            wall = time.perf_counter() - t0
            gen_before = s.adapter.generation
            if gc:
                gc.close()
            rows = writers * args.rows_per_writer
            row = {"writers": writers, "coalesced": coalesce, "rows": rows,
                   "generations_used": gen_before - gen0,
                   "wall_s": round(wall, 3),
                   "submit_ms": dist(lat),
                   "rows_per_s": round(rows / wall, 1),
                   "group": gc.stats() if gc else None}
            out.append(row)
            print(f"  writers={writers:<3} coalesce={coalesce!s:<5} "
                  f"submit p50 {row['submit_ms']['p50']:7.2f} p99 "
                  f"{row['submit_ms']['p99']:8.2f} ms | {rows} rows in "
                  f"{row['generations_used']} generations, "
                  f"{row['rows_per_s']:8.1f} rows/s"
                  + (f" (max group {row['group']['max_group']})" if gc else ""),
                  flush=True)
            s.close()
    return 0, {"mode": "groupcommit", "records": out,
               "max_delay_s": args.max_delay, "max_batch": args.max_batch}


def _drive_writers(store, gc, n_writers: int, rows: int) -> list[float]:
    """`n_writers` threads each submitting `rows` single-row writes."""
    import threading

    lat: list[float] = []
    lock = threading.Lock()

    def one(w: int) -> None:
        mine: list[float] = []
        for i in range(rows):
            t = time.perf_counter()
            if gc is not None:
                gc.assert_edge(f"g{w}_{i}", f"h{w}_{i}", "W",
                               vt_s=2_000_000 + w * rows + i)
            else:
                store.assert_edge(f"g{w}_{i}", f"h{w}_{i}", "W",
                                  vt_s=2_000_000 + w * rows + i)
            mine.append((time.perf_counter() - t) * 1e3)
        with lock:
            lat.extend(mine)

    threads = [threading.Thread(target=one, args=(w,)) for w in range(n_writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return lat


# --------------------------------------------------------------------------- #
# mode: residency — the D-045 index budget                                     #
# --------------------------------------------------------------------------- #


def cmd_residency(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    """What a resident TCSR costs a mixed read workload, per path query.

    D-045 measured the tax (18% on a later scan) and the rebuild (~400 ms)
    but declined to choose a budget, because choosing needs a workload mix.
    This runs the mix at several scan-per-path-query ratios, in one fresh
    process per condition, keeping the index and dropping it.
    """
    data, d = R.ensure_dataset(Path(args.workdir), args.scale)
    store = Path(args.store) if args.store else R.ensure_store(d, "native", data.log)
    qs = {q["id"]: q for q in R.queries_for(data, (PATH_QUERY, SCAN_QUERY))}
    ratios = [int(x) for x in args.scans_per_path.split(",")]
    out = []
    for ratio in ratios:
        for keep in (True, False):
            per_trial = []
            for _ in range(args.trials):
                cfg = {"store": str(store), "scans_per_path": ratio,
                       "rounds": args.rounds, "keep_index": keep,
                       "path": qs[PATH_QUERY], "scan": qs[SCAN_QUERY]}
                r = _collect(_spawn("residency", cfg), 3600)
                if r.get("failed"):
                    per_trial.append(r)
                    continue
                per_trial.append(r)
            good = [t for t in per_trial if not t.get("failed")]
            row = {"scans_per_path": ratio, "keep_index": keep,
                   "trials": per_trial,
                   "round_ms_p50": [t["round_ms"]["p50"] for t in good],
                   "path_ms_p50": [t["path_ms"]["p50"] for t in good],
                   "scan_ms_p50": [t["scan_ms"]["p50"] for t in good]}
            out.append(row)
            print(f"  scans/path={ratio:<3} keep={keep!s:<5} "
                  f"round p50 {row['round_ms_p50']} | "
                  f"path {row['path_ms_p50']} scan {row['scan_ms_p50']}",
                  flush=True)
    return 0, {"mode": "residency", "store": str(store), "scale": args.scale,
               "path_query": PATH_QUERY, "scan_query": SCAN_QUERY,
               "rounds": args.rounds, "records": out}


def child_residency(cfg: dict[str, Any]) -> dict[str, Any]:
    """One path query then `scans_per_path` scans, `rounds` times.

    `keep_index` is the knob under test, so it is *asserted* rather than
    trusted (lessons §13): with keep off, the adapter must not be holding a
    TCSR when the scans run.
    """
    import tgms
    from tgms.temporal.algebra import call_operator, ensure_all_registered

    ensure_all_registered()
    store = tgms.open(Path(cfg["store"]), backend="native", read_only=True)
    a = store.adapter
    keep = bool(cfg["keep_index"])
    path_q, scan_q = cfg["path"], cfg["scan"]

    call_operator(a, scan_q["op"], dict(scan_q["args"]))     # warm the scan
    round_ms, path_ms, scan_ms = [], [], []
    for _ in range(int(cfg["rounds"])):
        t_round = time.perf_counter()
        t = time.perf_counter()
        call_operator(a, path_q["op"], dict(path_q["args"]))
        path_ms.append((time.perf_counter() - t) * 1e3)
        assert a._tcsr is not None, "the path query did not build the index"
        if not keep:
            a._tcsr = None                     # the control, applied
            assert a._tcsr is None
        for _ in range(int(cfg["scans_per_path"])):
            t = time.perf_counter()
            call_operator(a, scan_q["op"], dict(scan_q["args"]))
            scan_ms.append((time.perf_counter() - t) * 1e3)
        if keep:
            assert a._tcsr is not None, "the index was dropped despite keep"
        round_ms.append((time.perf_counter() - t_round) * 1e3)
    store.close()
    return {"keep_index": keep, "scans_per_path": cfg["scans_per_path"],
            "round_ms": dist(round_ms), "path_ms": dist(path_ms),
            "scan_ms": dist(scan_ms)}


# --------------------------------------------------------------------------- #
# entry                                                                       #
# --------------------------------------------------------------------------- #


CHILDREN = {"reader": child_reader, "writer": child_writer,
            "residency": child_residency}


def _provenance() -> dict[str, Any]:
    """Enough to say whether two runs are comparable (spec §8.4)."""
    import platform

    def sh(*cmd: str) -> str:
        try:
            return subprocess.run(cmd, cwd=H.ROOT, capture_output=True,
                                  text=True, check=True).stdout.strip()
        except Exception:  # noqa: BLE001
            return "unknown"

    return {"commit": sh("git", "rev-parse", "--short", "HEAD"),
            "dirty": bool(sh("git", "status", "--porcelain", "-uno")),
            "python": platform.python_version(),
            "platform": f"{platform.system()} {platform.release()} "
                        f"{platform.machine()}",
            "cpu_count": os.cpu_count()}


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "_child":
        cfg = json.loads(sys.stdin.read())
        print(json.dumps(CHILDREN[sys.argv[2]](cfg)))
        return 0

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mode", choices=["mixed", "commitcost", "groupcommit",
                                     "residency"])
    ap.add_argument("--store", default="",
                    help="prebuilt store; default is the cached §14.4 one")
    ap.add_argument("--workdir", default="benchmarks/work-concurrency")
    ap.add_argument("--scale", type=int, default=1_000_000)
    ap.add_argument("--readers", default="1,2,4,8")
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--duration", type=float, default=30.0,
                    help="seconds each reader/writer runs inside the window")
    ap.add_argument("--lead", type=float, default=25.0,
                    help="barrier lead time: open + one warm pass")
    ap.add_argument("--batch-rows", type=int, default=100,
                    help="rows per writer commit in the mixed mode")
    ap.add_argument("--seed-rows", type=int, default=100_000)
    ap.add_argument("--commits", type=int, default=200)
    ap.add_argument("--batch-sizes", default="1,10,100,1000")
    ap.add_argument("--writers", default="1,2,4,8")
    ap.add_argument("--rows-per-writer", type=int, default=100)
    ap.add_argument("--max-delay", type=float, default=0.002)
    ap.add_argument("--max-batch", type=int, default=1000)
    ap.add_argument("--scans-per-path", default="1,4,16,64")
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    print(f"# {args.mode}", flush=True)
    t0 = time.time()
    rc, payload = {"mixed": cmd_mixed, "commitcost": cmd_commitcost,
                   "groupcommit": cmd_groupcommit,
                   "residency": cmd_residency}[args.mode](args)
    payload["wall_s"] = round(time.time() - t0, 1)
    payload["provenance"] = _provenance()
    if args.json:
        Path(args.json).write_text(json.dumps(payload, indent=1))
        print(f"wrote {args.json}", flush=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
