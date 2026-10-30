#!/usr/bin/env python3
"""Assemble the P-AXES axis B record (work when only k of N artifacts are
read per burst) from the timed `--refresh-on-read k` runs of
`scripts/bench_correction_storm.py`, and score the crossover k* against the
maintained view's per-burst cost on record.

Inputs:

- `--runs-dir`: one directory per run, named `<cell_id>-k<k>`, each holding
  the CLI's manifest `storm-<store>-0.json` and rows
  `storm-<store>-0-rows.jsonl`;
- `--view-rows`: `benchmarks/external-v1/ivm-differential-2026-10-07-rows.jsonl`
  (the view's per-burst `refresh_plus_publish_ms`; the view is not re-run);
- `--smoke`: a JSON document of the untimed collegemsg smoke runs before and
  after the engine change (copied into the record as context).

Per run and batch the rows keep the on-demand fields and the batch's own
context (candidates, changed, the tgms-L1 invalidated count); the summary
holds, per cell and k, the median and mean per-burst cost (`statistics.median`,
the same definition used for the view's per-burst cost), and per cell the
crossover k* by linear interpolation on the k grid, against the view's
median (primary) and mean (sensitivity). A k* below the grid's first point
is reported as `none` when TGMS's cost at k = 1 already exceeds the view's
(no crossing at any k >= 1), and as `above_grid` when TGMS stays below the
view's cost even with every artifact read.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import sys
from pathlib import Path
from typing import Any

PREREGISTRATION_B = (
    "P-AXES, frozen 2026-10-30T00:50:31Z. Axis B (work when only k of N artifacts are read "
    "per burst): the view pays its full maintenance cost per burst regardless of k "
    "(refresh_plus_publish per burst on record: ~4-7 ms collegemsg, 64-266 ms synth at 10^3, "
    "5.35 s at 10^4). TGMS with on-demand refresh pays, per burst, k x (one check, plus one "
    "refresh when the check says possibly stale). Measure TGMS's on-demand cost per read "
    "artifact (a harness mode that refreshes only the artifacts read, k drawn uniformly per "
    "burst) at k in {1, 10, 100, N} for N = 10^3 (both stores) and k in {1, 10, 100, 1,000, "
    "N} for the probe, and compute the crossover k* where TGMS's per-burst cost equals the "
    "view's. Prediction: k* in [3, 30] at N = 10^3 on synth, k* in [100, 1,000] at N = 10^4, "
    "and <= 3 on collegemsg (the view's per-burst cost there is only ~5 ms). Falsifier: k* < 1 "
    "on synth at either N -> TGMS loses even when a single artifact is read, and the paper "
    "says the view dominates on cost in every regime."
)
ADDENDUM = (
    "Addendum AXES-A, frozen 2026-10-30T01:29:22Z: phase 2 measures the engine-change tip "
    "(UNDECIDABLE on a known overlap, the shared per-burst fingerprint, incremental chain-cache "
    "extension on append); the cd194679 smoke numbers are reported as 'before'; the crossover "
    "k* is computed against the view's median per-burst cost (primary), mean as sensitivity."
)

#: (cell id, store, N requested, batches, TTF mode, k grid; the last k is N)
CELLS: tuple[tuple[str, str, int, int, str, tuple[int, ...]], ...] = (
    ("collegemsg-c1-none-n1000-s0", "collegemsg", 1000, 20, "end-to-end", (1, 10, 100, 1000)),
    ("synth-iv-60k-c1-none-n1000-s0", "synth-iv-60k", 1000, 20, "end-to-end",
     (1, 10, 100, 1000)),
    ("synth-iv-60k-c1-none-n10000-s0", "synth-iv-60k", 10000, 5, "sum",
     (1, 10, 100, 1000, 10000)),
)

ROW_FIELDS = ("batch_index", "n_registered", "candidate_survivors", "changed_count",
              "on_demand_k", "on_demand_ms", "on_demand_reads", "on_demand_refreshes",
              "on_demand_refresh_errors", "on_demand_check_cache_misses", "check_cache_misses",
              "ttf_mode", "correction_class", "correction_generator")


def _sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode("utf-8")).hexdigest()


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _need(cond: bool, what: str) -> None:
    if not cond:
        raise ValueError(what)


def crossover(ks: list[int], cost: list[float], view: float) -> dict[str, Any]:
    """k* where TGMS's per-burst cost reaches `view`, by linear interpolation
    between grid points. `ks` are the effective read counts (k clamped to the
    registered count), increasing."""
    if cost[0] >= view:
        return {"k_star": None, "kind": "none",
                "note": "TGMS's cost at k = 1 already reaches the view's: no crossing at k >= 1"}
    for (k0, c0), (k1, c1) in zip(zip(ks, cost), zip(ks[1:], cost[1:])):
        if c0 < view <= c1:
            return {"k_star": k0 + (view - c0) * (k1 - k0) / (c1 - c0), "kind": "interpolated",
                    "between": [k0, k1]}
    return {"k_star": None, "kind": "above_grid",
            "note": "TGMS stays below the view's cost with every artifact read"}


def view_per_burst(view_rows: list[dict[str, Any]], cell_id: str) -> dict[str, Any]:
    row = next(r for r in view_rows if r["cell_id"] == cell_id)
    vals = [b["refresh_plus_publish_ms"] for b in row["result"]["per_burst"]]
    return {"bursts": len(vals), "median_ms": statistics.median(vals),
            "mean_ms": statistics.fmean(vals), "min_ms": min(vals), "max_ms": max(vals)}


def run_rows(cell_id: str, store: str, n: int, batches: int, ttf: str, k: int,
             manifest: dict[str, Any], rows: list[dict[str, Any]], git_commit: str,
             ) -> list[dict[str, Any]]:
    cfg = manifest["config"]
    where = f"{cell_id} k={k}"
    _need(manifest["git_commit"] == git_commit, f"{where}: git_commit {manifest['git_commit']}")
    _need(cfg["refresh_on_read"] == k and cfg["n_artifacts"] == n and cfg["seed"] == 0,
          f"{where}: config")
    _need(cfg["mix"] == "c1" and cfg["check_cache"] == "chain" and cfg["measure_ttf"] == ttf
          and cfg["backend"] == "native" and cfg["burst_size"] == 10000
          and cfg["burst_after"] == 10, f"{where}: the committed flags")
    _need(len(rows) == batches == cfg["batches"], f"{where}: {len(rows)} batches")
    _need(not cfg.get("wall_capped"), f"{where}: wall-capped")
    out = []
    for r in rows:
        n_reg = r["n_registered"]
        _need(r["on_demand_k"] == k and r["on_demand_reads"] == min(k, n_reg),
              f"{where}: reads at batch {r['batch_index']}")
        _need(r["on_demand_refresh_errors"] == 0, f"{where}: refresh errors")
        _need(r["arms"]["tgms-L1"]["false_fresh_count"] == 0, f"{where}: tgms-L1 false-fresh")
        row = {f: r.get(f) for f in ROW_FIELDS}
        row.update({"cell_id": cell_id, "store": store, "k": k,
                    "tgms_l1_invalidated": r["arms"]["tgms-L1"]["invalidated_count"],
                    "run_wall_s": cfg["wall_s"]})
        out.append(row)
    return out


def summarize(rows: list[dict[str, Any]], views: dict[str, dict[str, Any]]) -> dict[str, Any]:
    per_cell: dict[str, Any] = {}
    for cell_id, _store, _n, _b, _ttf, grid in CELLS:
        per_k = {}
        for k in grid:
            rs = [r for r in rows if r["cell_id"] == cell_id and r["k"] == k]
            ms = [r["on_demand_ms"] for r in rs]
            reads = sum(r["on_demand_reads"] for r in rs)
            per_k[str(k)] = {
                "k_effective": rs[0]["on_demand_reads"], "batches": len(rs),
                "median_ms": statistics.median(ms), "mean_ms": statistics.fmean(ms),
                "min_ms": min(ms), "max_ms": max(ms),
                "reads": reads, "refreshes": sum(r["on_demand_refreshes"] for r in rs),
                "ms_per_read_mean": sum(ms) / reads,
                "median_ms_per_read": statistics.median(
                    [r["on_demand_ms"] / r["on_demand_reads"] for r in rs]),
                "run_wall_s": rs[0]["run_wall_s"],
            }
        ks = [per_k[str(k)]["k_effective"] for k in grid]
        med = [per_k[str(k)]["median_ms"] for k in grid]
        mean = [per_k[str(k)]["mean_ms"] for k in grid]
        v = views[cell_id]
        k1 = [r for r in rows if r["cell_id"] == cell_id and r["k"] == 1]
        first = [r["on_demand_ms"] for r in k1 if r["on_demand_refreshes"] == 0]
        per_cell[cell_id] = {
            "per_k": per_k, "view_per_burst": v,
            # primary: TGMS median vs view median; sensitivity: means on both sides
            "crossover_median": crossover(ks, med, v["median_ms"]),
            "crossover_mean": crossover(ks, mean, v["mean_ms"]),
            "first_check_ms": {"batches": len(first),
                               "median": statistics.median(first) if first else None},
        }
    first_all = [r["on_demand_ms"] for r in rows if r["k"] == 1 and r["on_demand_refreshes"] == 0]
    return {"per_cell": per_cell,
            "first_check_ms_median_all_cells": statistics.median(first_all),
            "first_check_batches_all_cells": len(first_all)}


def verdict(summary: dict[str, Any]) -> dict[str, Any]:
    """The pre-registered predictions, read off the primary (median) crossovers.
    A k* is a number when the curves cross inside the grid, else the text
    `none` (TGMS above the view already at k = 1) or `above_grid`."""
    pc = summary["per_cell"]

    def k_of(cell: str) -> Any:
        x = pc[cell]["crossover_median"]
        return x["k_star"] if x["kind"] == "interpolated" else x["kind"]

    def within(x: Any, lo: float, hi: float) -> bool:
        return isinstance(x, float) and lo <= x <= hi

    s1k = k_of("synth-iv-60k-c1-none-n1000-s0")
    s10k = k_of("synth-iv-60k-c1-none-n10000-s0")
    col = k_of("collegemsg-c1-none-n1000-s0")
    return {
        "k_star_synth_n1k": s1k, "k_star_synth_n10k": s10k, "k_star_collegemsg_n1k": col,
        "synth_n1k_in_3_30": within(s1k, 3, 30),
        "synth_n10k_in_100_1000": within(s10k, 100, 1000),
        "collegemsg_at_most_3": within(col, 0, 3) or col == "none",
        "falsifier_k_star_below_1_on_synth": "none" in (s1k, s10k),
    }


def build(runs: dict[tuple[str, int], tuple[dict[str, Any], list[dict[str, Any]], dict[str, str]]],
          view_rows: list[dict[str, Any]], smoke: dict[str, Any], *, git_commit: str,
          timestamp_utc: str, host_block: dict[str, Any], date: str, rows_relpath: str,
          provenance: dict[str, Any], view_rows_sha256: str,
          ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    run_files: dict[str, Any] = {}
    digests: dict[str, str] = {}
    for cell_id, store, n, batches, ttf, grid in CELLS:
        for k in grid:
            manifest, rws, shas = runs[(cell_id, k)]
            rows += run_rows(cell_id, store, n, batches, ttf, k, manifest, rws, git_commit)
            run_files[f"{cell_id}-k{k}"] = {**shas, "wall_s": manifest["config"]["wall_s"],
                                            "timestamp_utc": manifest["timestamp_utc"],
                                            "result_digest": manifest["result_digest"]}
            digests.setdefault(f"store {store} (eventlog sha256 at run end, k={k}, {cell_id})",
                               manifest["dataset"]["digest"])
    views = {c: view_per_burst(view_rows, c) for c, *_ in CELLS}
    summary = summarize(rows, views)
    summary["verdict"] = verdict(summary)
    summary["smoke_untimed"] = smoke
    record = {
        "schema_version": "1.0.0",
        "git_commit": git_commit,
        "timestamp_utc": timestamp_utc,
        "machine": host_block,
        "config": {"campaign": "axes-on-demand", "axis": "B", "date": date,
                   "cells": [{"cell_id": c, "store": s, "n_artifacts": n, "batches": b,
                              "measure_ttf": t, "k_grid": list(g)} for c, s, n, b, t, g in CELLS],
                   "flags": "--seed 0 --mix c1 --backend native --burst-size 10000 "
                            "--burst-after 10 --check-cache chain --refresh-on-read k "
                            "(--allow-r18-trip at 10^4)",
                   "view_per_burst_source": "benchmarks/external-v1/ivm-differential-2026-10-07"
                                            "-rows.jsonl, per_burst[].refresh_plus_publish_ms",
                   "view_rows_sha256": view_rows_sha256,
                   "median_definition": "statistics.median on both sides",
                   "runs": run_files, **provenance},
        "preregistration": PREREGISTRATION_B,
        "addendum": ADDENDUM,
        "seed": {"value": 0},
        "dataset": {"name": "collegemsg and synth-iv-60k same-host control stores (copies), "
                            "seed-0 c1-none populations at N = 10^3 and 10^4",
                    "digest": _sha(digests), "digest_kind": "manifest",
                    "components": digests},
        "result_digest": _sha(rows),
        "protocol": {"warmups": 0, "reps": 1,
                     "ceilings": {"note": "one run per (cell, k); box quiet, Memgraph stopped"}},
        "record": rows_relpath,
        "n_cells": len(CELLS),
        "summary": summary,
    }
    return record, rows


def load_runs(runs_dir: Path) -> dict[tuple[str, int], tuple[dict, list, dict]]:
    out = {}
    for cell_id, store, *_rest, grid in CELLS:
        for k in grid:
            d = runs_dir / f"{cell_id}-k{k}"
            man = d / f"storm-{store}-0.json"
            rws = d / f"storm-{store}-0-rows.jsonl"
            out[(cell_id, k)] = (
                json.loads(man.read_text(encoding="utf-8")),
                [json.loads(x) for x in rws.read_text(encoding="utf-8").splitlines() if x.strip()],
                {"manifest_sha256": _sha_file(man), "rows_sha256": _sha_file(rws)})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--runs-dir", required=True, type=Path)
    ap.add_argument("--view-rows", required=True, type=Path)
    ap.add_argument("--smoke", required=True, type=Path)
    ap.add_argument("--provenance", required=True, type=Path)
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--date", required=True)
    ap.add_argument("--git-commit", required=True)
    ap.add_argument("--timestamp-utc", required=True)
    ap.add_argument("--host", default="xzgpu")
    ap.add_argument("--platform", required=True)
    ap.add_argument("--cpus", type=int, required=True)
    ap.add_argument("--ram-gb", type=float, required=True)
    ap.add_argument("--rows-relpath", required=True)
    a = ap.parse_args(argv)
    _need(re.fullmatch(r"[0-9a-f]{40}", a.git_commit) is not None, "--git-commit: full sha")
    view_rows = [json.loads(x) for x in a.view_rows.read_text(encoding="utf-8").splitlines()
                 if x.strip()]
    record, rows = build(
        load_runs(a.runs_dir), view_rows, json.loads(a.smoke.read_text(encoding="utf-8")),
        git_commit=a.git_commit, timestamp_utc=a.timestamp_utc,
        host_block={"host": a.host, "platform": a.platform, "cpus": a.cpus, "ram_gb": a.ram_gb},
        date=a.date, rows_relpath=a.rows_relpath,
        provenance=json.loads(a.provenance.read_text(encoding="utf-8")),
        view_rows_sha256=_sha_file(a.view_rows))
    a.out_dir.mkdir(parents=True, exist_ok=True)
    rec_path = a.out_dir / f"axes-on-demand-{a.date}.json"
    rows_path = a.out_dir / f"axes-on-demand-{a.date}-rows.jsonl"
    rec_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    rows_path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows),
                         encoding="utf-8")
    print(f"wrote {rec_path}\nwrote {rows_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
