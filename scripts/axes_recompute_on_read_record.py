#!/usr/bin/env python3
"""Assemble the P-ROR record (recompute on read, the third configuration of
the regime comparison) from the timed `--refresh-on-read k
--refresh-on-read-policy always` runs of `scripts/bench_correction_storm.py`,
the same-day `check`-policy controls, and the P-AXES axis B record.

Three configurations are compared per burst on the on-demand protocol of
axis B (one correction per burst, k of the registered artifacts read, drawn
uniformly):

- **TGMS on demand** (`check` policy): per read one level-1 check, then one
  refresh only when the check does not say fresh. Its per-k cost is the axis
  B record's (`axes-on-demand-2026-10-30-rows.jsonl`, all k, all three
  cells); this lane re-runs it at k = 1 and k = N on the two N = 10^3 cells as
  same-day controls (they also carry `correction_apply_ms`).
- **Recompute on read** (`always` policy): per read the artifact's plan is
  re-run and republished unconditionally, no check, always exact.
- **The maintained view**: its per-burst cost is `refresh_plus_publish_ms` of
  `ivm-differential-2026-10-07-rows.jsonl` (not re-run), as in axis B.

Two timer scopes, for every TGMS configuration: *reads only* (the interval
`on_demand_ms`, as on record) and *reads plus applying the correction*
(`on_demand_ms + correction_apply_ms`, per burst; the scope Table 1 uses for
the view). Each is reported by median and by mean over bursts. The
`correction_apply_ms` of a burst is the same correction's write whatever the
policy or k (the draws do not depend on either), so the axis B record's `check`
runs, which predate the field, are put on the second scope as an *estimate*
(their `on_demand_ms` plus the same burst's `correction_apply_ms` from a
reference run of the same cell: the `check` control at k = 1 on the N = 10^3
cells, the `always` k = 1 run on the probe); every number built that way is
named `estimate` in the record. The crossover k* (linear interpolation on the
k grid, as in axis B) is reported for each configuration, scope and basis, and
the regime gain is the ratio of the two TGMS configurations' k*.

The record holds numbers and the thresholds the pre-registration names beside
them (`prediction_inputs`); it states no verdict.
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from axes_on_demand_record import CELLS, crossover, view_per_burst  # noqa: E402

PREREGISTRATION_ROR = (
    "P-ROR, frozen 2026-10-30T07:56:05Z. The regime claim compares TGMS on demand (check, then "
    "refresh only when flagged) with a maintained view; it lacks the obvious third "
    "configuration, *recompute on read*: every read artifact's plan is re-run and republished "
    "unconditionally, no lineage check, always exact. Measure it on the on-demand protocol of "
    "P-AXES axis B (one correction per burst, k reads drawn uniformly, same stores and k grids, "
    "N = 10^3 both stores and the probe; Memgraph stopped, box quiet; code = the pushed tip "
    "after lane T19), timed as the same interval. For every TGMS configuration report two timer "
    "scopes: reads only (as on record) and reads plus applying the correction (the log append; "
    "the scope Table 1 uses for the view), and both median and mean. Predictions: (1) "
    "recompute-on-read's cost per read equals the per-artifact refresh on record within 30 % "
    "(15.3 synth / 3.4 collegemsg / 15.65 probe ms); (2) at k = N TGMS on demand is cheaper per "
    "read by >= 2x on synth 10^3 and the probe (6.30 / 10.56 ms on record) and >= 2.5x on "
    "collegemsg (0.93 ms); (3) at k = 1 recompute-on-read is cheaper on collegemsg (~3.4 vs 4.9 "
    "ms) and TGMS is cheaper on synth (4.9 vs ~15.3); (4) against the view (median basis) "
    "recompute-on-read's crossover is ~7 on synth 10^3, ~1.8 on collegemsg and ~340 on the "
    "probe, so lineage extends the regime by >= 2.5x on synth 10^3, >= 1.5x on the probe, and "
    "by less than 1.5x on collegemsg. Falsifier: TGMS on demand costs at least as much as "
    "recompute-on-read at every measured k on either synth size."
)

#: The thresholds and reference values the pre-registration states, by cell.
REF_REFRESH_MS = {"collegemsg-c1-none-n1000-s0": 3.4, "synth-iv-60k-c1-none-n1000-s0": 15.3,
                  "synth-iv-60k-c1-none-n10000-s0": 15.65}
REF_TGMS_PER_READ_AT_N = {"collegemsg-c1-none-n1000-s0": 0.93,
                          "synth-iv-60k-c1-none-n1000-s0": 6.30,
                          "synth-iv-60k-c1-none-n10000-s0": 10.56}
PRED2_MIN_RATIO = {"collegemsg-c1-none-n1000-s0": 2.5, "synth-iv-60k-c1-none-n1000-s0": 2.0,
                   "synth-iv-60k-c1-none-n10000-s0": 2.0}
PRED4_PREDICTED_K = {"collegemsg-c1-none-n1000-s0": 1.8, "synth-iv-60k-c1-none-n1000-s0": 7.0,
                     "synth-iv-60k-c1-none-n10000-s0": 340.0}
PRED4_GAIN_BOUND = {"collegemsg-c1-none-n1000-s0": ("below", 1.5),
                    "synth-iv-60k-c1-none-n1000-s0": ("at_least", 2.5),
                    "synth-iv-60k-c1-none-n10000-s0": ("at_least", 1.5)}

#: (cell id, policy) -> the k values this lane runs. The `check` controls are
#: short runs at k = 1 and k = N on the two N = 10^3 cells only.
ALWAYS_GRID = {c: g for c, _s, _n, _b, _t, g in CELLS}
CONTROL_CELLS = ("collegemsg-c1-none-n1000-s0", "synth-iv-60k-c1-none-n1000-s0")

ROW_FIELDS = ("batch_index", "n_registered", "candidate_survivors", "changed_count",
              "on_demand_k", "on_demand_ms", "on_demand_reads", "on_demand_refreshes",
              "on_demand_refresh_errors", "on_demand_check_cache_misses", "check_cache_misses",
              "correction_apply_ms", "ttf_mode", "correction_class", "correction_generator")


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode("utf-8")).hexdigest()


def _need(cond: bool, what: str) -> None:
    if not cond:
        raise ValueError(what)


def _stats(vals: list[float]) -> dict[str, float]:
    return {"median_ms": statistics.median(vals), "mean_ms": statistics.fmean(vals),
            "min_ms": min(vals), "max_ms": max(vals)}


def run_rows(cell_id: str, store: str, n: int, batches: int, ttf: str, policy: str, k: int,
             manifest: dict[str, Any], rows: list[dict[str, Any]], git_commit: str,
             ) -> list[dict[str, Any]]:
    """Validate one run against the committed flags and return its record rows."""
    cfg = manifest["config"]
    where = f"{cell_id} {policy} k={k}"
    _need(manifest["git_commit"] == git_commit, f"{where}: git_commit {manifest['git_commit']}")
    _need(cfg["refresh_on_read"] == k and cfg["n_artifacts"] == n and cfg["seed"] == 0,
          f"{where}: config")
    _need(cfg.get("refresh_on_read_policy") == policy, f"{where}: policy in the manifest")
    _need(cfg["mix"] == "c1" and cfg["check_cache"] == "chain" and cfg["measure_ttf"] == ttf
          and cfg["backend"] == "native" and cfg["burst_size"] == 10000
          and cfg["burst_after"] == 10, f"{where}: the committed flags")
    _need(len(rows) == batches == cfg["batches"], f"{where}: {len(rows)} batches")
    _need(not cfg.get("wall_capped"), f"{where}: wall-capped")
    out = []
    for r in rows:
        n_reg = r["n_registered"]
        b = r["batch_index"]
        _need(r["on_demand_k"] == k and r["on_demand_reads"] == min(k, n_reg),
              f"{where}: reads at batch {b}")
        _need(r["on_demand_refresh_errors"] == 0, f"{where}: refresh errors at batch {b}")
        _need(r["arms"]["tgms-L1"]["false_fresh_count"] == 0, f"{where}: tgms-L1 false-fresh")
        _need(isinstance(r.get("correction_apply_ms"), float) and r["correction_apply_ms"] > 0,
              f"{where}: correction_apply_ms at batch {b}")
        if policy == "always":
            _need(r.get("on_demand_policy") == "always", f"{where}: row policy at batch {b}")
            _need(r["on_demand_refreshes"] == r["on_demand_reads"],
                  f"{where}: every read refreshed at batch {b}")
            _need(r["on_demand_check_cache_misses"] == 0, f"{where}: no check at batch {b}")
        else:
            _need("on_demand_policy" not in r, f"{where}: check rows carry no policy key")
            _need(r["on_demand_refreshes"] <= r["on_demand_reads"], f"{where}: refreshes")
        row = {f: r.get(f) for f in ROW_FIELDS}
        row.update({"cell_id": cell_id, "store": store, "k": k, "policy": policy,
                    "tgms_l1_invalidated": r["arms"]["tgms-L1"]["invalidated_count"],
                    "run_wall_s": cfg["wall_s"]})
        out.append(row)
    return out


def _by_batch(rows: list[dict[str, Any]], field: str) -> dict[int, float]:
    return {r["batch_index"]: r[field] for r in rows}


def series(rows: list[dict[str, Any]], apply_ref: dict[int, float] | None = None,
           ) -> dict[str, list[float]]:
    """Per-burst series for one run's rows: reads only, and reads plus the burst's
    correction apply (the row's own, or `apply_ref`'s for the same burst index)."""
    reads_only = [r["on_demand_ms"] for r in rows]
    if apply_ref is None:
        with_apply = [r["on_demand_ms"] + r["correction_apply_ms"] for r in rows]
    else:
        with_apply = [r["on_demand_ms"] + apply_ref[r["batch_index"]] for r in rows]
    return {"reads_only": reads_only, "with_apply": with_apply}


def _per_k_block(rows: list[dict[str, Any]], apply_ref: dict[int, float] | None) -> dict[str, Any]:
    s = series(rows, apply_ref)
    reads = sum(r["on_demand_reads"] for r in rows)
    ms = s["reads_only"]
    out: dict[str, Any] = {
        "k_effective": rows[0]["on_demand_reads"], "batches": len(rows),
        "reads_only": _stats(ms), "with_apply": _stats(s["with_apply"]),
        "reads": reads, "refreshes": sum(r["on_demand_refreshes"] for r in rows),
        "ms_per_read_mean": sum(ms) / reads,
        "median_ms_per_read": statistics.median(
            [r["on_demand_ms"] / r["on_demand_reads"] for r in rows]),
    }
    if apply_ref is None:
        out["apply_ms"] = _stats([r["correction_apply_ms"] for r in rows])
        out["with_apply_kind"] = "measured"
    else:
        out["with_apply_kind"] = "estimate"
    return out


def _cross(ks: list[int], blocks: list[dict[str, Any]], scope: str, view: dict[str, Any],
           ) -> dict[str, Any]:
    return {
        "median": crossover(ks, [b[scope]["median_ms"] for b in blocks], view["median_ms"]),
        "mean": crossover(ks, [b[scope]["mean_ms"] for b in blocks], view["mean_ms"]),
    }


def _gain(tgms: dict[str, Any], ror: dict[str, Any]) -> dict[str, Any]:
    if tgms["kind"] == "interpolated" and ror["kind"] == "interpolated":
        return {"value": tgms["k_star"] / ror["k_star"], "kind": "ratio"}
    return {"value": None, "kind": "undefined",
            "note": f"TGMS k* is {tgms['kind']}, recompute-on-read k* is {ror['kind']}"}


def summarize(ror_rows: list[dict[str, Any]], axes_rows: list[dict[str, Any]],
              views: dict[str, dict[str, Any]]) -> dict[str, Any]:
    per_cell: dict[str, Any] = {}
    for cell_id, _store, _n, _b, _ttf, grid in CELLS:
        v = views[cell_id]

        def pick(policy: str, k: int, cell_id: str = cell_id) -> list[dict[str, Any]]:
            return sorted((r for r in ror_rows if r["cell_id"] == cell_id and r["policy"] == policy
                           and r["k"] == k), key=lambda r: r["batch_index"])

        def record(k: int, cell_id: str = cell_id) -> list[dict[str, Any]]:
            return sorted((r for r in axes_rows if r["cell_id"] == cell_id and r["k"] == k),
                          key=lambda r: r["batch_index"])

        # the apply reference for the estimate on the axis B `check` runs
        ref_rows = pick("check", 1) if cell_id in CONTROL_CELLS else pick("always", 1)
        _need(len(ref_rows) > 0, f"{cell_id}: apply reference run")
        apply_ref = _by_batch(ref_rows, "correction_apply_ms")
        per_k: dict[str, Any] = {}
        always_blocks, check_blocks, ks = [], [], []
        for k in grid:
            al = pick("always", k)
            rec = record(k)
            _need(len(al) > 0 and len(rec) > 0, f"{cell_id} k={k}: rows")
            _need(len(al) == len(rec), f"{cell_id} k={k}: batch counts differ")
            _need([r["on_demand_reads"] for r in al] == [r["on_demand_reads"] for r in rec],
                  f"{cell_id} k={k}: reads differ between the lane and the record")
            _need({r["batch_index"] for r in al} <= set(apply_ref), f"{cell_id}: apply ref batches")
            a_blk = _per_k_block(al, None)
            c_blk = _per_k_block(rec, apply_ref)
            entry: dict[str, Any] = {"k_effective": a_blk["k_effective"], "always": a_blk,
                                     "check_record": c_blk,
                                     "ratio_always_to_check_median_reads_only":
                                         a_blk["reads_only"]["median_ms"]
                                         / c_blk["reads_only"]["median_ms"]}
            ctrl = pick("check", k)
            if ctrl:
                entry["check_control"] = _per_k_block(ctrl, None)
                entry["check_control_vs_record_median_reads_only"] = (
                    entry["check_control"]["reads_only"]["median_ms"]
                    / c_blk["reads_only"]["median_ms"])
            per_k[str(k)] = entry
            always_blocks.append(a_blk)
            check_blocks.append(c_blk)
            ks.append(a_blk["k_effective"])
        crossovers: dict[str, Any] = {}
        for scope in ("reads_only", "with_apply"):
            t = _cross(ks, check_blocks, scope, v)
            a = _cross(ks, always_blocks, scope, v)
            crossovers[scope] = {
                "tgms_check": t, "recompute_on_read": a,
                "regime_gain": {"median": _gain(t["median"], a["median"]),
                                "mean": _gain(t["mean"], a["mean"])},
            }
        crossovers["with_apply_tgms_kind"] = "estimate (record on_demand_ms + reference apply)"
        n_at = ks[-1]
        per_cell[cell_id] = {
            "n_registered": n_at, "per_k": per_k, "view_per_burst": v, "crossover": crossovers,
            "per_read_at_n": {
                "tgms_check_record_median": per_k[str(grid[-1])]["check_record"]
                ["median_ms_per_read"],
                "recompute_on_read_median": per_k[str(grid[-1])]["always"]["median_ms_per_read"],
                "ratio_recompute_to_tgms": per_k[str(grid[-1])]["always"]["median_ms_per_read"]
                / per_k[str(grid[-1])]["check_record"]["median_ms_per_read"],
                "tgms_check_record_mean": per_k[str(grid[-1])]["check_record"]["ms_per_read_mean"],
                "recompute_on_read_mean": per_k[str(grid[-1])]["always"]["ms_per_read_mean"],
                "ratio_recompute_to_tgms_mean": per_k[str(grid[-1])]["always"]["ms_per_read_mean"]
                / per_k[str(grid[-1])]["check_record"]["ms_per_read_mean"],
            },
        }
    return {"per_cell": per_cell, "prediction_inputs": prediction_inputs(per_cell)}


def prediction_inputs(per_cell: dict[str, Any]) -> dict[str, Any]:
    """The numbers each pre-registered prediction reads, beside its threshold.
    No pass or fail is computed here."""
    out: dict[str, Any] = {}
    for cell_id, _s, _n, _b, _t, grid in CELLS:
        pc = per_cell[cell_id]
        kN, k1 = str(grid[-1]), str(grid[0])
        al_n = pc["per_k"][kN]["always"]
        x = pc["crossover"]["reads_only"]
        out[cell_id] = {
            "p1_recompute_ms_per_read_at_k_N": {
                "median_of_ratios": al_n["median_ms_per_read"],
                "mean_total_over_reads": al_n["ms_per_read_mean"],
                "reference_per_artifact_refresh_ms": REF_REFRESH_MS[cell_id],
                "ratio_median_to_reference": al_n["median_ms_per_read"] / REF_REFRESH_MS[cell_id],
                "ratio_mean_to_reference": al_n["ms_per_read_mean"] / REF_REFRESH_MS[cell_id],
                "band": [0.7, 1.3]},
            "p2_tgms_cheaper_per_read_at_k_N": {
                "recompute_over_tgms_ratio": pc["per_read_at_n"]["ratio_recompute_to_tgms"],
                "recompute_over_tgms_ratio_mean": pc["per_read_at_n"]["ratio_recompute_to_tgms_mean"],
                "tgms_ms_per_read_on_record_per_preregistration":
                    REF_TGMS_PER_READ_AT_N[cell_id],
                "min_ratio": PRED2_MIN_RATIO[cell_id]},
            "p3_per_burst_at_k_1_median_reads_only": {
                "recompute_on_read_ms": pc["per_k"][k1]["always"]["reads_only"]["median_ms"],
                "tgms_check_record_ms": pc["per_k"][k1]["check_record"]["reads_only"]["median_ms"],
                "recompute_on_read_mean_ms": pc["per_k"][k1]["always"]["reads_only"]["mean_ms"],
                "tgms_check_record_mean_ms":
                    pc["per_k"][k1]["check_record"]["reads_only"]["mean_ms"]},
            "p4_crossover_and_gain_median_reads_only": {
                "recompute_on_read_k_star": x["recompute_on_read"]["median"],
                "tgms_check_k_star": x["tgms_check"]["median"],
                "regime_gain": x["regime_gain"]["median"],
                "predicted_recompute_k_star": PRED4_PREDICTED_K[cell_id],
                "gain_bound": list(PRED4_GAIN_BOUND[cell_id])},
            "falsifier_per_k_recompute_over_tgms_median_reads_only": {
                str(k): pc["per_k"][str(k)]["ratio_always_to_check_median_reads_only"]
                for k in grid},
        }
    return out


def build(runs: dict[tuple[str, str, int], tuple[dict[str, Any], list[dict[str, Any]],
                                                   dict[str, str]]],
          axes_record: dict[str, Any], axes_rows: list[dict[str, Any]],
          view_rows: list[dict[str, Any]], smoke: dict[str, Any], *, git_commit: str,
          timestamp_utc: str, host_block: dict[str, Any], date: str, rows_relpath: str,
          provenance: dict[str, Any], input_shas: dict[str, str],
          ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    run_files: dict[str, Any] = {}
    digests: dict[str, set[str]] = {}
    for (cell_id, policy, k), (manifest, rws, shas) in sorted(runs.items()):
        _cid, store, n, batches, ttf, _g = next(c for c in CELLS if c[0] == cell_id)
        rows += run_rows(cell_id, store, n, batches, ttf, policy, k, manifest, rws, git_commit)
        run_files[f"{cell_id}-k{k}-{policy}"] = {
            **shas, "wall_s": manifest["config"]["wall_s"],
            "timestamp_utc": manifest["timestamp_utc"],
            "result_digest": manifest["result_digest"]}
        digests.setdefault(f"store {store} ({cell_id}, eventlog sha256 at run end)",
                           set()).add(manifest["dataset"]["digest"])
    # the lane's grid is complete: always at the whole k grid, check at k = 1 and k = N (10^3)
    for cell_id, _s, _n, _b, _t, grid in CELLS:
        for k in grid:
            _need((cell_id, "always", k) in runs, f"missing run: {cell_id} always k={k}")
        if cell_id in CONTROL_CELLS:
            for k in (grid[0], grid[-1]):
                _need((cell_id, "check", k) in runs, f"missing control: {cell_id} check k={k}")
    views = {c: view_per_burst(view_rows, c) for c, *_ in CELLS}
    # the axis B record's rows are the `check` policy on record: they must be the file its
    # own record describes, and its view numbers must be the ones used here
    _need(axes_record["result_digest"] == _sha(axes_rows), "axis B rows != the axis B record")
    for cell_id, *_r in CELLS:
        _need(axes_record["summary"]["per_cell"][cell_id]["view_per_burst"]["median_ms"]
              == views[cell_id]["median_ms"], f"{cell_id}: view median differs from axis B's")
    summary = summarize(rows, axes_rows, views)
    # the recomputed `check` crossovers on the record must be the axis B record's
    for cell_id, *_r in CELLS:
        mine = summary["per_cell"][cell_id]["crossover"]["reads_only"]["tgms_check"]
        theirs = axes_record["summary"]["per_cell"][cell_id]
        for basis, key in (("median", "crossover_median"), ("mean", "crossover_mean")):
            _need(mine[basis]["kind"] == theirs[key]["kind"], f"{cell_id}: {basis} crossover kind")
            if mine[basis]["kind"] == "interpolated":
                _need(abs(mine[basis]["k_star"] - theirs[key]["k_star"])
                      <= 1e-9 * abs(theirs[key]["k_star"]), f"{cell_id}: {basis} k*")
    summary["same_log_digest_across_the_cells_runs"] = {
        key: len(v) == 1 for key, v in sorted(digests.items())}
    summary["smoke_untimed"] = smoke
    record = {
        "schema_version": "1.0.0",
        "git_commit": git_commit,
        "timestamp_utc": timestamp_utc,
        "machine": host_block,
        "config": {
            "campaign": "axes-recompute-on-read", "date": date,
            "cells": [{"cell_id": c, "store": s, "n_artifacts": n, "batches": b,
                       "measure_ttf": t, "k_grid": list(g)} for c, s, n, b, t, g in CELLS],
            "policies": {"always": "every read artifact is refreshed and republished, no check "
                                   "(recompute on read)",
                         "check": "one level-1 check per read, one refresh when it does not "
                                  "say fresh (TGMS on demand)"},
            "flags": "--seed 0 --mix c1 --backend native --burst-size 10000 --burst-after 10 "
                     "--check-cache chain --refresh-on-read k --refresh-on-read-policy "
                     "{always,check} (--allow-r18-trip at 10^4)",
            "check_policy_source": "benchmarks/external-v1/axes-on-demand-2026-10-30-rows.jsonl "
                                   "(all k, all cells, reads only) and this lane's same-day "
                                   "controls (k = 1 and k = N, N = 10^3 cells, both scopes)",
            "view_per_burst_source": "benchmarks/external-v1/ivm-differential-2026-10-07"
                                     "-rows.jsonl, per_burst[].refresh_plus_publish_ms",
            "timer_scopes": {"reads_only": "on_demand_ms",
                             "with_apply": "on_demand_ms + correction_apply_ms, per burst"},
            "correction_apply_definition": "wall time of Storm._write for the burst's correction "
                                           "(event-log append, chain extension, the adapter's "
                                           "operation application and commit), outside every "
                                           "other interval",
            "median_definition": "statistics.median on both sides",
            "inputs_sha256": input_shas,
            "runs": run_files, **provenance},
        "preregistration": PREREGISTRATION_ROR,
        "seed": {"value": 0},
        "dataset": {"name": "collegemsg and synth-iv-60k same-host control stores (copies), "
                            "seed-0 c1-none populations at N = 10^3 and 10^4",
                    "digest": _sha({k: sorted(v) for k, v in digests.items()}),
                    "digest_kind": "manifest",
                    "components": {k: sorted(v) for k, v in sorted(digests.items())}},
        "result_digest": _sha(rows),
        "protocol": {"warmups": 0, "reps": 1,
                     "ceilings": {"note": "one run per (cell, policy, k); box quiet, "
                                          "Memgraph stopped"}},
        "record": rows_relpath,
        "n_cells": len(CELLS),
        "summary": summary,
    }
    return record, rows


def load_runs(runs_dir: Path) -> dict[tuple[str, str, int], tuple[dict, list, dict]]:
    out = {}
    for cell_id, store, *_rest, grid in CELLS:
        n = next(c[2] for c in CELLS if c[0] == cell_id)
        for policy in ("always", "check"):
            for k in grid:
                d = runs_dir / f"{store}-c1-none-n{n}-s0-k{k}-{policy}"
                man = d / f"storm-{store}-0.json"
                rws = d / f"storm-{store}-0-rows.jsonl"
                if not man.exists():
                    continue
                out[(cell_id, policy, k)] = (
                    json.loads(man.read_text(encoding="utf-8")),
                    [json.loads(x) for x in rws.read_text(encoding="utf-8").splitlines()
                     if x.strip()],
                    {"manifest_sha256": _sha_file(man), "rows_sha256": _sha_file(rws)})
    return out


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--runs-dir", required=True, type=Path)
    ap.add_argument("--axes-record", required=True, type=Path)
    ap.add_argument("--axes-rows", required=True, type=Path)
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
    record, rows = build(
        load_runs(a.runs_dir), json.loads(a.axes_record.read_text(encoding="utf-8")),
        _jsonl(a.axes_rows), _jsonl(a.view_rows), json.loads(a.smoke.read_text(encoding="utf-8")),
        git_commit=a.git_commit, timestamp_utc=a.timestamp_utc,
        host_block={"host": a.host, "platform": a.platform, "cpus": a.cpus, "ram_gb": a.ram_gb},
        date=a.date, rows_relpath=a.rows_relpath,
        provenance=json.loads(a.provenance.read_text(encoding="utf-8")),
        input_shas={"axes_record": _sha_file(a.axes_record), "axes_rows": _sha_file(a.axes_rows),
                    "view_rows": _sha_file(a.view_rows)})
    a.out_dir.mkdir(parents=True, exist_ok=True)
    rec_path = a.out_dir / f"axes-recompute-on-read-{a.date}.json"
    rows_path = a.out_dir / f"axes-recompute-on-read-{a.date}-rows.jsonl"
    rec_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    rows_path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows),
                         encoding="utf-8")
    print(f"wrote {rec_path}\nwrote {rows_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
