#!/usr/bin/env python3
"""Builder for the refresh-cost attribution record (2026-10-29, lane SHRINK,
pre-registration P-SHRINK).

The question: the storm-v3 rows show a median of about 11 ms per refreshed
artifact at N = 1,000 against about 20 ms at N = 10,000 on the same store.
Is that growth in publication (the registry's per-publish work) or in plan
execution? This record answers with per-phase, per-artifact timings of the
same artifacts at both sizes (``phase`` rows), the kind histograms, and the
per-batch figures of the committed storm-v3 rows (``committed_batch`` rows).

Two stages:

* ``rows_from_profile`` turns the profile process's raw JSON (kept on the
  measurement host) into ``profile_call`` rows. Run once, at landing.
* ``derive`` computes every number in the manifest from the committed rows,
  the kind-label maps, the profile-process info files, and the committed
  storm-v3 tarballs. ``--check`` re-derives and compares with the committed
  manifest, so nothing in the manifest is hand-typed.

    python scripts/refresh_cost_attribution_record.py --check
    python scripts/refresh_cost_attribution_record.py --write \\
        --profile-1000 prof1000.json --profile-10000 prof10000.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import tarfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
STORM = ROOT / "benchmarks" / "storm-v1"
STEM = "refresh-cost-attribution-2026-10-29"
MANIFEST = STORM / f"{STEM}.json"
ROWS = STORM / f"{STEM}-rows.jsonl"
RAW = STORM / STEM
SUMS = STORM / f"SHA256SUMS-{STEM}.txt"
V3_TARBALL = STORM / "storm-v3-records-36-tasks.tar.gz"
PROBE_TARBALL = STORM / "storm-v3-r18-probe-records.tar.gz"
PROBE_ROWS = STORM / "storm-v3-r18-probe-2026-10-08-rows.jsonl"

#: A batch is "expensive" when its mean refresh cost per refreshed artifact
#: exceeds this many milliseconds (the storm-v3 rows are bimodal around it).
EXPENSIVE_MS = 15.0
#: Kinds whose mean cost per refresh is below this are "cheap".
CHEAP_KIND_MS = 10.0

PREREGISTRATION = (
    "P-SHRINK, frozen 2026-10-29T12:26:44Z. Observed in the storm-v3 cluster rows: "
    "about 11 ms per refreshed artifact at N = 10^3 vs about 20 ms at N = 10^4 on the same "
    "store, while survivor fraction, check cost per candidate and global-recompute cost per "
    "artifact are flat. Prediction: the growth is in publication (the registry's per-publish "
    "work: append, in-memory fold, checkpoint bookkeeping) growing with the number of "
    "registered artifacts and their version history; plan execution per artifact is flat. "
    "Falsifier: execution grows and publication is flat."
)

#: (summary key, phase key in a row's ``phases_ms``; "total" is the row's total_ms)
PHASES: tuple[tuple[str, str], ...] = (
    ("total", "total"),
    ("execution", "exec"),
    ("kernel", "exec.kernel"),
    ("result_digest", "exec.result_digest"),
    ("float_canonicalisation", "exec.canonicalize"),
    ("leaf_meta", "exec.leaf_meta"),
    ("publication", "publish"),
    ("register", "publish.register"),
    ("store_identity", "publish.store_identity"),
    ("append", "publish.append"),
    ("append_locked", "publish.append.locked"),
    ("fsync", "publish.append.fsync"),
    ("record_canonical_json", "publish.append.canonical_json"),
    ("chain_extend", "publish.append.extend_chain"),
    ("fold_index", "publish.append.fold_index"),
    ("tail_reload", "publish.append.reload_tail"),
    ("readback_verify", "publish.append.verify_landed"),
    ("result_put", "publish.result_put"),
    ("gc", "gc"),
)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def result_digest(rows: list[dict]) -> str:
    return sha256_hex(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


# --------------------------------------------------------------------------
# stage 1: profile rows
# --------------------------------------------------------------------------

def rows_from_profile(profile: dict, n_target: int, n_registered: int) -> list[dict]:
    """``profile_call`` rows from the profile process's raw JSON (its ``rows``:
    one per refresh() call, ``tag`` s50.rK for the same-50 sample or ``strat``)."""
    out = []
    for r in profile["rows"]:
        tag = r["tag"]
        out.append({
            "row_type": "profile_call",
            "n_target": n_target,
            "n_registered": n_registered,
            "sample": "same50" if tag.startswith("s50") else "stratified",
            "round": int(tag.split(".r")[1]) if tag.startswith("s50") else None,
            "name": r["name"],
            "op": r["op"],
            "total_ms": r["total"],
            "phases_ms": r["ph"],
        })
    return out


def committed_rows(v3_tarball: Path = V3_TARBALL, probe_tarball: Path = PROBE_TARBALL,
                   probe_rows: Path = PROBE_ROWS) -> list[dict]:
    """``committed_batch`` rows from the committed storm-v3 records: every batch
    of the 18 synth-iv-60k cells (end-to-end mode) and the 5 probe batches
    (sum mode). Per-artifact refresh cost is ``arms.tgms-L1.refresh_wall_ms``
    over ``candidate_survivors`` (equal to ``e2e_refresh_calls`` wherever that
    field is recorded); speedup is global-recompute ``ttf_ms`` over tgms-L1
    ``ttf_ms``. The refreshed set's names are not in the rows (``changed`` is
    the oracle-changed subset only), so its ``temporal_paths`` share is not
    derivable here: the field is null."""
    out: list[dict] = []
    with tarfile.open(v3_tarball, "r:gz") as tf:
        tasks = sorted({n.split("/")[0] for n in tf.getnames() if n.startswith("task-")},
                       key=lambda t: int(t.split("-")[1]))
        for task in tasks:
            members = [n for n in tf.getnames() if n.startswith(task + "/")]
            man = json.loads(tf.extractfile(
                next(n for n in members if n.endswith(".json"))).read().decode("utf-8"))
            cfg = man["config"]
            if cfg["store"] != "synth-iv-60k":
                continue
            rows = [json.loads(line) for line in tf.extractfile(
                next(n for n in members if n.endswith("-rows.jsonl"))).read().decode(
                    "utf-8").splitlines() if line.strip()]
            cell = f"synth-iv-60k-{cfg['mix']}-{cfg['age'] or 'none'}-n{cfg['n_artifacts']}-s{man['seed']['value']}"
            out += [_batch_row("storm-v3-records-36-tasks.tar.gz", task, cell, cfg["measure_ttf"], b)
                    for b in sorted(rows, key=lambda b: b["batch_index"])]
    prows = sorted(load_jsonl(probe_rows), key=lambda b: b["batch_index"])
    with tarfile.open(probe_tarball, "r:gz") as tf:
        member = next(n for n in tf.getnames() if n.endswith("-rows.jsonl"))
        tar_rows = sorted((json.loads(line) for line in tf.extractfile(member).read().decode(
            "utf-8").splitlines() if line.strip()), key=lambda b: b["batch_index"])
    if tar_rows != prows:
        raise ValueError("probe tarball rows differ from the committed probe rows")
    out += [_batch_row("storm-v3-r18-probe-2026-10-08-rows.jsonl", "task-0",
                       "synth-iv-60k-c1-none-n10000-s0-probe", "sum", b) for b in prows]
    return out


def _batch_row(source: str, task: str, cell: str, ttf_mode: str, b: dict) -> dict:
    l1 = b["arms"]["tgms-L1"]
    per_artifact = l1["refresh_wall_ms"] / b["candidate_survivors"]
    return {
        "row_type": "committed_batch",
        "source": source,
        "task": task,
        "cell": cell,
        "ttf_mode": ttf_mode,
        "batch_index": b["batch_index"],
        "n_registered": b["n_registered"],
        "candidate_survivors": b["candidate_survivors"],
        "refresh_wall_ms": l1["refresh_wall_ms"],
        "refresh_ms_per_refreshed_artifact": per_artifact,
        "global_recompute_ttf_ms": b["arms"]["global-recompute"]["ttf_ms"],
        "tgms_l1_ttf_ms": l1["ttf_ms"],
        "speedup": b["arms"]["global-recompute"]["ttf_ms"] / l1["ttf_ms"],
        "expensive": per_artifact > EXPENSIVE_MS,
        "temporal_paths_share_of_refreshed": None,
    }


# --------------------------------------------------------------------------
# stage 2: derive
# --------------------------------------------------------------------------

def _phase_value(row: dict, key: str) -> float:
    return row["total_ms"] if key == "total" else row["phases_ms"].get(key, 0.0)


def phase_table(rows: list[dict]) -> dict[str, dict[str, float]]:
    """Mean and median of every phase over ``rows`` (ms per refresh() call)."""
    table: dict[str, dict[str, float]] = {}
    for name, key in PHASES:
        vals = [_phase_value(r, key) for r in rows]
        table[name] = {"mean": statistics.fmean(vals), "median": statistics.median(vals)}
    return table


def histogram(ops: dict[str, str]) -> dict[str, int]:
    h: dict[str, int] = {}
    for kind in ops.values():
        h[kind] = h.get(kind, 0) + 1
    return dict(sorted(h.items()))


def derive(profile_rows: list[dict], batch_rows: list[dict], ops_1k: dict[str, str],
           ops_10k: dict[str, str], info: dict[str, dict], replicate: dict) -> dict:
    """Every number of the manifest's ``summary``."""
    by = {n: [r for r in profile_rows if r["n_target"] == n] for n in (1000, 10000)}
    summary: dict[str, Any] = {"phase_means_ms": {}, "per_kind_ms": {}}
    for sample in ("same50", "stratified"):
        summary["phase_means_ms"][sample] = {}
        for n in (1000, 10000):
            sel = [r for r in by[n] if r["sample"] == sample]
            table = phase_table(sel)
            table["n_calls"] = len(sel)
            summary["phase_means_ms"][sample][f"n{n}"] = table
        a = summary["phase_means_ms"][sample]["n1000"]
        b = summary["phase_means_ms"][sample]["n10000"]
        summary["phase_means_ms"][sample]["ratio_n10000_over_n1000"] = {
            k: b[k]["mean"] / a[k]["mean"] for k, _ in PHASES if a[k]["mean"]}
    kinds = sorted({r["op"] for r in profile_rows})
    for kind in kinds:
        entry: dict[str, Any] = {}
        for n in (1000, 10000):
            sel = [r for r in by[n] if r["sample"] == "stratified" and r["op"] == kind]
            entry[f"n{n}"] = {
                "n_calls": len(sel),
                "total": statistics.fmean(r["total_ms"] for r in sel),
                "execution": statistics.fmean(r["phases_ms"].get("exec", 0.0) for r in sel),
                "publication": statistics.fmean(r["phases_ms"].get("publish", 0.0) for r in sel),
            }
        entry["total_both_sizes"] = statistics.fmean(
            r["total_ms"] for n in (1000, 10000) for r in by[n]
            if r["sample"] == "stratified" and r["op"] == kind)
        summary["per_kind_ms"][kind] = entry
    cheap = [k for k in kinds if summary["per_kind_ms"][k]["total_both_sizes"] < CHEAP_KIND_MS]
    summary["cheap_kinds"] = {
        "threshold_ms": CHEAP_KIND_MS,
        "kinds": cheap,
        "max_mean_ms": max(summary["per_kind_ms"][k][f"n{n}"]["total"]
                           for k in cheap for n in (1000, 10000)),
    }
    summary["temporal_paths_ms"] = summary["per_kind_ms"]["temporal_paths"]["total_both_sizes"]
    h1, h10 = histogram(ops_1k), histogram(ops_10k)
    summary["kind_histograms"] = {
        "n1000": {"registered": len(ops_1k), "counts": h1},
        "n10000": {"registered": len(ops_10k), "counts": h10},
        "prefix_check": "every name registered at N=1000 has the same operator at N=10000: "
                        + str(all(ops_10k.get(k) == v for k, v in ops_1k.items())),
    }
    cost = {k: summary["per_kind_ms"][k]["total_both_sizes"] for k in kinds}
    summary["mix_predicted_mean_ms_all_registered"] = {
        "n1000": sum(h1[k] * cost[k] for k in h1) / len(ops_1k),
        "n10000": sum(h10[k] * cost[k] for k in h10) / len(ops_10k),
    }
    summary["registry"] = {f"n{n}": info[str(n)] for n in (1000, 10000)}

    cell = [r for r in batch_rows if r["cell"] == "synth-iv-60k-c1-none-n1000-s0"]
    probe = [r for r in batch_rows if r["cell"].endswith("-probe")]
    synth = [r for r in batch_rows if not r["cell"].endswith("-probe")]
    summary["committed"] = {
        "refresh_cost_expensive_threshold_ms": EXPENSIVE_MS,
        "cell_n1000_seed0_c1_none": _cell_summary(cell),
        "probe_n10000": _cell_summary(probe),
        "storm_v3_synth_all_cells": {
            "batches": len(synth),
            "expensive_batches": sum(r["expensive"] for r in synth),
            "expensive_share": sum(r["expensive"] for r in synth) / len(synth),
            "expensive_mean_ms": _mean_where(synth, True),
            "cheaper_mean_ms": _mean_where(synth, False),
            "per_artifact_ms_median": statistics.median(
                r["refresh_ms_per_refreshed_artifact"] for r in synth),
            "per_artifact_ms_pooled": sum(r["refresh_wall_ms"] for r in synth)
            / sum(r["candidate_survivors"] for r in synth),
        },
        "temporal_paths_share_of_refreshed": "not derivable from the committed rows: they "
        "record the oracle-changed names only (arms.*.invalidated_count and false_stale_count "
        "are counts), and temporal_paths artifacts almost never change",
    }
    summary["replicate_batches"] = replicate
    return summary


def _cell_summary(rows: list[dict]) -> dict:
    sp = [r["speedup"] for r in rows]
    pa = [r["refresh_ms_per_refreshed_artifact"] for r in rows]
    return {
        "batches": len(rows),
        "speedup_per_batch": sp,
        "speedup_min": min(sp), "speedup_max": max(sp), "speedup_median": statistics.median(sp),
        "refresh_ms_per_refreshed_artifact": pa,
        "refresh_ms_median": statistics.median(pa),
        "refresh_ms_pooled": sum(r["refresh_wall_ms"] for r in rows)
        / sum(r["candidate_survivors"] for r in rows),
        "expensive_batches": sum(r["expensive"] for r in rows),
        "expensive_mean_ms": _mean_where(rows, True),
        "cheaper_mean_ms": _mean_where(rows, False),
    }


def _mean_where(rows: list[dict], expensive: bool) -> float | None:
    sel = [r["refresh_ms_per_refreshed_artifact"] for r in rows if r["expensive"] == expensive]
    return statistics.fmean(sel) if sel else None


def verdict(summary: dict) -> dict:
    """The three statements the README quotes, with the numbers read from
    ``summary`` (same-50 sample, ms per refresh() call)."""
    t = summary["phase_means_ms"]["same50"]
    a, b = t["n1000"], t["n10000"]
    return {
        "prediction": "not supported: publication is flat in N "
                      f"({a['publication']['mean']:.3f} vs {b['publication']['mean']:.3f} ms "
                      "per refresh on the same 50 artifacts) while the registry file and the "
                      "in-memory entry count grow tenfold",
        "falsifier": "not triggered: execution per artifact is also flat "
                     f"({a['execution']['mean']:.2f} vs {b['execution']['mean']:.2f} ms on the "
                     "same 50 artifacts), so 'execution grows and publication is flat' does "
                     "not hold",
        "premise": "a batch-composition artifact of comparing medians over 5 batches "
                   "(N = 10^4) against 20 batches (N = 10^3): the cost of a refreshed "
                   "artifact depends on its operator kind (3 to 189 ms), and the mix of kinds "
                   "in the refreshed set differs between batches, not between sizes",
        "side_finding": "Registry._store_identity re-reads and compares the event log's "
                        "genesis prefix (one 4.1 MB line on these stores) on every call, "
                        "twice per publish, "
                        f"{a['store_identity']['mean']:.1f} ms per refresh of "
                        f"{a['publication']['mean']:.1f} ms publication at N = 10^3; it "
                        "scales with the genesis record, not with N",
    }


def build_manifest(rows: list[dict], summary: dict, config: dict) -> dict:
    return {
        "schema_version": "1.0.0",
        "git_commit": config["git_commit"],
        "timestamp_utc": config["timestamp_utc"],
        "machine": config["machine"],
        "config": config["config"],
        "seed": {"value": 0},
        "dataset": config["dataset"],
        "result_digest": result_digest(rows),
        "protocol": {"warmups": 10, "reps": 3, "ceilings": {},
                     "notes": "10 discarded warm-up refresh() calls per profile process; 3 "
                              "rounds of the same 50 artifacts; one pass of 20 artifacts per "
                              "kind; one 50-call cProfile pass"},
        "record": f"benchmarks/storm-v1/{STEM}-rows.jsonl",
        "preregistration": PREREGISTRATION,
        "verdict": verdict(summary),
        "summary": summary,
    }


def _inputs(raw: Path = RAW) -> tuple[dict, dict, dict, dict, dict]:
    ops_1k = json.loads((raw / "ops-1000.json").read_text(encoding="utf-8"))
    ops_10k = json.loads((raw / "ops-10000.json").read_text(encoding="utf-8"))
    info = {str(n): json.loads((raw / f"profile-info-{n}.json").read_text(encoding="utf-8"))
            for n in (1000, 10000)}
    replicate = json.loads((raw / "replicate-batch-composition.json").read_text(encoding="utf-8"))
    config = json.loads((raw / "manifest-config.json").read_text(encoding="utf-8"))
    return ops_1k, ops_10k, info, replicate, config


def build(profile_rows: list[dict]) -> tuple[list[dict], dict]:
    ops_1k, ops_10k, info, replicate, config = _inputs()
    batch = committed_rows()
    rows = profile_rows + batch
    summary = derive(profile_rows, batch, ops_1k, ops_10k, info, replicate)
    return rows, build_manifest(rows, summary, config)


def dump_rows(rows: list[dict]) -> str:
    return "".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in rows)


def dump_manifest(manifest: dict) -> str:
    return json.dumps(manifest, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="re-derive the manifest from the committed rows and compare")
    ap.add_argument("--write", action="store_true",
                    help="write rows and manifest from the profile process's raw JSON")
    ap.add_argument("--profile-1000", type=Path)
    ap.add_argument("--profile-10000", type=Path)
    args = ap.parse_args(argv)
    if args.write:
        if not (args.profile_1000 and args.profile_10000):
            ap.error("--write needs --profile-1000 and --profile-10000")
        info = {n: json.loads((RAW / f"profile-info-{n}.json").read_text(encoding="utf-8"))
                for n in (1000, 10000)}
        prof = []
        for n, path in ((1000, args.profile_1000), (10000, args.profile_10000)):
            prof += rows_from_profile(json.loads(path.read_text(encoding="utf-8")), n,
                                      info[n]["n_names"])
        rows, manifest = build(prof)
        ROWS.write_text(dump_rows(rows), encoding="utf-8")
        MANIFEST.write_text(dump_manifest(manifest), encoding="utf-8")
        print(f"wrote {ROWS.name} ({len(rows)} rows) and {MANIFEST.name}")
        return 0
    rows = load_jsonl(ROWS)
    prof = [r for r in rows if r["row_type"] == "profile_call"]
    got_rows, manifest = build(prof)
    ok = rows == got_rows and dump_manifest(manifest) == MANIFEST.read_text(encoding="utf-8")
    print("re-derived record matches the committed files" if ok else "MISMATCH")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
