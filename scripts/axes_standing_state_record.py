#!/usr/bin/env python3
"""Assemble the P-AXES axis A record (standing state) from the outputs of
`scripts/axes_standing_state.py`.

Inputs, per measured cell: the `view` document (`view-standing-state.json`,
which embeds `ivm-dd`'s own `state-report.json` and the 1 s samples) and the
`tgms` document. The assembler checks that both sides hold the same
population (the view's artifact count equals TGMS's registered count, and
the sha256 of TGMS's sorted registered names equals the one computed from
the exported cell's `artifacts.jsonl`, passed in) and that every reading it
reports is present, then writes

  <out-dir>/axes-standing-state-<date>.json        (summary + provenance)
  <out-dir>/axes-standing-state-<date>-rows.jsonl  (one row per configuration
                                                     per cell)

Host paths in the inputs are dropped: rows name cells and stores only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

PREREGISTRATION = (
    "P-AXES, frozen 2026-10-30T00:50:31Z. Axis A (standing state): resident memory of the "
    "view process after loading the changelog and registering N artifacts, steady after one "
    "burst (RSS, and the dataflow's own state accounting if it reports it), versus TGMS's "
    "standing cost for the same N: registry file bytes, in-memory registry entries, and the "
    "check process's RSS after registration. Prediction: the view's resident memory grows at "
    "least linearly with N and at N = 10^4 is >= 10x TGMS's; TGMS's registry is ~4 KB per "
    "artifact and its process RSS is within 2x between 10^3 and 10^4. Falsifier: the ratio at "
    "10^4 < 2x -> the standing-state axis does not favour TGMS and the paper says so."
)

#: The cells, in record order: (cell id, store, N requested, TGMS TTF mode).
CELLS: tuple[tuple[str, str, int, str], ...] = (
    ("collegemsg-c1-none-n1000-s0", "collegemsg", 1000, "end-to-end"),
    ("synth-iv-60k-c1-none-n1000-s0", "synth-iv-60k", 1000, "end-to-end"),
    ("synth-iv-60k-c1-none-n10000-s0", "synth-iv-60k", 10000, "sum"),
)

VIEW_CHECKPOINTS = ("after_bundle_load", "after_load_and_registration", "after_burst_1", "end")
CHECK_READINGS = ("after_imports", "after_registry_load", "after_check_pass")


def _sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode("utf-8")).hexdigest()


def _need(cond: bool, what: str) -> None:
    if not cond:
        raise ValueError(what)


def view_row(cell_id: str, store: str, n: int, doc: dict[str, Any]) -> dict[str, Any]:
    _need(doc.get("returncode") == 0, f"{cell_id}: ivm-dd exited {doc.get('returncode')}")
    rep = doc["state_report"]
    _need(rep is not None and rep["cell_id"] == cell_id, f"{cell_id}: state report cell id")
    _need(rep["workers"] == 1, f"{cell_id}: single worker")
    cps = {c["label"]: c for c in rep["checkpoints"]}
    _need(tuple(c["label"] for c in rep["checkpoints"]) == VIEW_CHECKPOINTS,
          f"{cell_id}: checkpoint labels {list(cps)}")
    for label in VIEW_CHECKPOINTS[1:]:
        _need((cps[label]["arranged_records"] or 0) > 0, f"{cell_id}: {label} arranged records")
    samples = doc["samples"]
    return {
        "configuration": "ivm-dd", "cell_id": cell_id, "store": store,
        "n_artifacts_requested": n, "n_registered": rep["artifacts"], "bursts": rep["bursts"],
        "workers": rep["workers"], "load_ms": rep["load_ms"],
        "checkpoints": rep["checkpoints"],
        "sampled": {"interval_s": doc["interval_s"], "n_samples": len(samples),
                    "vm_rss_kb_max": doc["sampled_rss_kb_max"],
                    "samples": [{k: s[k] for k in ("t_s", "vm_rss_kb", "vm_hwm_kb")}
                                for s in samples]},
        "wall_s": doc["wall_s"],
    }


def tgms_row(cell_id: str, store: str, n: int, ttf: str, doc: dict[str, Any],
             export_names_sha256: str) -> dict[str, Any]:
    _need(doc.get("check_process_returncode") == 0, f"{cell_id}: check process failed")
    reg = doc["registration"]
    _need(reg["n_artifacts_requested"] == n, f"{cell_id}: N requested")
    _need(doc["measure_ttf"] == ttf and doc["check_cache"] == "chain" and doc["mix"] == "c1",
          f"{cell_id}: committed flags")
    _need(doc["names_registered_sha256"] == export_names_sha256,
          f"{cell_id}: TGMS's registered names differ from the exported cell's artifacts")
    _need(reg["fold"]["entries"] == reg["n_registered"] == reg["registry_record_lines"],
          f"{cell_id}: one registry entry per registered artifact")
    chk = doc["check_process"]
    _need(chk["fold"] == {"names": reg["n_registered"], "entries": reg["n_registered"]},
          f"{cell_id}: check process folds the post-registration registry")
    for key in CHECK_READINGS:
        _need(isinstance(chk[key]["vm_rss_kb"], int), f"{cell_id}: check process {key}")
    return {
        "configuration": "tgms", "cell_id": cell_id, "store": store,
        "n_artifacts_requested": n, "n_registered": reg["n_registered"],
        "measure_ttf": ttf, "check_cache": "chain", "mix": "c1", "seed": doc["seed"],
        "registration": reg, "burst_1": doc["burst_1"],
        "harness_process": doc["harness_process"],
        "check_process": {k: chk[k] for k in (
            *CHECK_READINGS, "fold", "registry_load_s", "candidate_survivors", "checks",
            "not_fresh", "chain_cache", "check_pass_s")},
        "check_process_samples": {
            "n_samples": len(doc["check_process_samples"]),
            "vm_rss_kb_max": max((s["vm_rss_kb"] for s in doc["check_process_samples"]),
                                 default=None)},
        "names_registered_sha256": doc["names_registered_sha256"],
    }


def cell_summary(v: dict[str, Any], t: dict[str, Any]) -> dict[str, Any]:
    cps = {c["label"]: c for c in v["checkpoints"]}
    chk = t["check_process"]
    n = t["n_registered"]
    _need(v["n_registered"] == n, f"{v['cell_id']}: view holds {v['n_registered']} artifacts, "
          f"TGMS registered {n}")
    view_reg = cps["after_load_and_registration"]["vm_rss_kb"]
    view_b1 = cps["after_burst_1"]["vm_rss_kb"]
    t_reg = chk["after_registry_load"]["vm_rss_kb"]
    t_chk = chk["after_check_pass"]["vm_rss_kb"]
    t_peak = chk["after_check_pass"]["vm_hwm_kb"]
    fold_kb = t_reg - chk["after_imports"]["vm_rss_kb"]
    reg = t["registration"]
    return {
        "n_registered": n,
        "view_rss_kb_after_registration": view_reg,
        "view_rss_kb_after_burst_1": view_b1,
        "view_rss_kb_after_bundle_load": cps["after_bundle_load"]["vm_rss_kb"],
        "view_arranged_records_after_registration": cps["after_load_and_registration"]["arranged_records"],
        "view_arranged_records_after_burst_1": cps["after_burst_1"]["arranged_records"],
        "tgms_check_rss_kb_after_imports": chk["after_imports"]["vm_rss_kb"],
        "tgms_check_rss_kb_after_registry_load": t_reg,
        "tgms_check_rss_kb_after_check_pass": t_chk,
        "tgms_check_hwm_kb_after_check_pass": t_peak,
        "tgms_harness_rss_kb_after_registration": t["harness_process"]["after_registration"]["vm_rss_kb"],
        "tgms_harness_rss_kb_after_burst_1": t["harness_process"]["after_burst_1"]["vm_rss_kb"],
        "tgms_registry_bytes": reg["registry_bytes"],
        "tgms_registry_bytes_per_artifact": reg["registry_bytes"] / n,
        "tgms_registry_bytes_after_burst_1": t["burst_1"]["registry_bytes"],
        "tgms_registry_bytes_per_republish": (t["burst_1"]["registry_bytes"]
                                              - reg["registry_bytes"]) / n,
        "tgms_registry_mem_kb": fold_kb,
        "tgms_registry_mem_bytes_per_artifact": fold_kb * 1024 / n,
        "ratio_view_over_tgms_after_registration": view_reg / t_reg,
        "ratio_view_over_tgms_after_burst_1": view_b1 / t_chk,
        "ratio_view_over_tgms_peak_after_burst_1": view_b1 / t_peak,
        "ratio_view_over_tgms_harness_after_burst_1":
            view_b1 / t["harness_process"]["after_burst_1"]["vm_rss_kb"],
    }


def summarize(per_cell: dict[str, dict[str, Any]]) -> dict[str, Any]:
    s1k = per_cell["synth-iv-60k-c1-none-n1000-s0"]
    s10k = per_cell["synth-iv-60k-c1-none-n10000-s0"]
    n_growth = s10k["n_registered"] / s1k["n_registered"]
    view_growth = s10k["view_rss_kb_after_burst_1"] / s1k["view_rss_kb_after_burst_1"]
    arr_growth = (s10k["view_arranged_records_after_burst_1"]
                  / s1k["view_arranged_records_after_burst_1"])
    tg_reg = (s10k["tgms_check_rss_kb_after_registry_load"]
              / s1k["tgms_check_rss_kb_after_registry_load"])
    tg_chk = (s10k["tgms_check_rss_kb_after_check_pass"]
              / s1k["tgms_check_rss_kb_after_check_pass"])
    tg_peak = (s10k["tgms_check_hwm_kb_after_check_pass"]
               / s1k["tgms_check_hwm_kb_after_check_pass"])
    fold_growth = s10k["tgms_registry_mem_kb"] / s1k["tgms_registry_mem_kb"]
    ratio10k = s10k["ratio_view_over_tgms_after_burst_1"]
    disk = [c["tgms_registry_bytes_per_artifact"] for c in per_cell.values()]
    mem = [c["tgms_registry_mem_bytes_per_artifact"] for c in per_cell.values()]
    baseline = [c["tgms_check_rss_kb_after_imports"] for c in per_cell.values()]
    return {
        "per_cell": per_cell,
        "synth_growth_n1k_to_n10k": {
            "n_registered": n_growth, "view_rss_after_burst_1": view_growth,
            "view_arranged_records_after_burst_1": arr_growth,
            "tgms_check_rss_after_registry_load": tg_reg,
            "tgms_check_rss_after_check_pass": tg_chk,
            "tgms_check_peak_after_check_pass": tg_peak,
            "tgms_registry_mem": fold_growth,
        },
        "tgms_registry_bytes_per_artifact_range": [min(disk), max(disk)],
        "tgms_registry_mem_bytes_per_artifact_range": [min(mem), max(mem)],
        "tgms_check_rss_kb_after_imports_range": [min(baseline), max(baseline)],
        "ratio_view_over_tgms_n10k_after_burst_1": ratio10k,
        "predictions": {
            "view_grows_at_least_linearly": {
                "holds": view_growth >= n_growth,
                "reading": "sub-linear: view RSS x{:.2f} (arranged records x{:.2f}) for x{:.2f} "
                           "the artifacts".format(view_growth, arr_growth, n_growth)},
            "view_at_least_10x_tgms_at_n10k": {"holds": ratio10k >= 10, "ratio": ratio10k},
            "registry_about_4kb_per_artifact": {
                "reading": "on disk {:.2f}-{:.2f} KiB per artifact at registration (one "
                           "generation; append-only, it grows by about one record per "
                           "republish); in memory {:.2f}-{:.2f} KiB per artifact".format(
                               min(disk) / 1024, max(disk) / 1024,
                               min(mem) / 1024, max(mem) / 1024)},
            "tgms_rss_within_2x_n1k_to_n10k": {
                "holds": tg_reg <= 2 and tg_chk <= 2,
                "reading": "x{:.2f} after the registry load, x{:.2f} after the check pass, but "
                           "only because of the interpreter-and-imports baseline (~{:.0f} MiB); "
                           "the registry's own memory grows x{:.2f}".format(
                               tg_reg, tg_chk, sum(baseline) / len(baseline) / 1024,
                               fold_growth)},
        },
        "falsifier": {"condition": "view / TGMS RSS ratio at N = 10^4 < 2",
                      "triggered": ratio10k < 2},
    }


def build(views: dict[str, dict[str, Any]], tgms: dict[str, dict[str, Any]],
          export_names_sha256: dict[str, str], *, git_commit: str, timestamp_utc: str,
          host_block: dict[str, Any], date: str, rows_relpath: str,
          provenance: dict[str, Any], dataset_digests: dict[str, str],
          ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    per_cell: dict[str, dict[str, Any]] = {}
    for cell_id, store, n, ttf in CELLS:
        v = view_row(cell_id, store, n, views[cell_id])
        t = tgms_row(cell_id, store, n, ttf, tgms[cell_id], export_names_sha256[cell_id])
        rows += [v, t]
        per_cell[cell_id] = cell_summary(v, t)
    summary = summarize(per_cell)
    record = {
        "schema_version": "1.0.0",
        "git_commit": git_commit,
        "timestamp_utc": timestamp_utc,
        "machine": host_block,
        "config": {"campaign": "axes-standing-state", "axis": "A", "date": date,
                   "cells": [{"cell_id": c, "store": s, "n_artifacts": n,
                              "tgms_measure_ttf": ttf} for c, s, n, ttf in CELLS],
                   "untimed": True, "nice": 19, "sampling_interval_s": 1.0,
                   "units": "kB in rows and summary are /proc/<pid>/status kB (KiB)",
                   **provenance},
        "preregistration": PREREGISTRATION,
        "seed": {"value": 0},
        "dataset": {"name": "exported seed-0 c1-none cells (collegemsg N=10^3, synth-iv-60k "
                            "N=10^3 and N=10^4) and their source stores",
                    "digest": _sha(dataset_digests), "digest_kind": "manifest",
                    "components": dataset_digests},
        "result_digest": _sha(rows),
        "protocol": {"warmups": 0, "reps": 1,
                     "ceilings": {"note": "memory measurement; no timing claim"}},
        "record": rows_relpath,
        "n_cells": len(CELLS),
        "summary": summary,
    }
    return record, rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--standing-dir", required=True, type=Path,
                    help="holds view-<cell>/view-standing-state.json and tgms-<cell>.json")
    ap.add_argument("--export-names-sha256", required=True, type=Path,
                    help="JSON {cell_id: sha256 of the export's sorted artifact names}")
    ap.add_argument("--dataset-digests", required=True, type=Path,
                    help="JSON {label: sha256} of the exported cells and source event logs")
    ap.add_argument("--provenance", required=True, type=Path,
                    help="JSON object merged into config (binary hashes, toolchain)")
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

    views = {c: json.loads((a.standing_dir / f"view-{c}" / "view-standing-state.json")
                           .read_text(encoding="utf-8")) for c, *_ in CELLS}
    tgms = {c: json.loads((a.standing_dir / f"tgms-{c}.json").read_text(encoding="utf-8"))
            for c, *_ in CELLS}
    record, rows = build(
        views, tgms, json.loads(a.export_names_sha256.read_text(encoding="utf-8")),
        git_commit=a.git_commit, timestamp_utc=a.timestamp_utc,
        host_block={"host": a.host, "platform": a.platform, "cpus": a.cpus, "ram_gb": a.ram_gb},
        date=a.date, rows_relpath=a.rows_relpath,
        provenance=json.loads(a.provenance.read_text(encoding="utf-8")),
        dataset_digests=json.loads(a.dataset_digests.read_text(encoding="utf-8")))
    a.out_dir.mkdir(parents=True, exist_ok=True)
    rec_path = a.out_dir / f"axes-standing-state-{a.date}.json"
    rows_path = a.out_dir / f"axes-standing-state-{a.date}-rows.jsonl"
    rec_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    rows_path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows),
                         encoding="utf-8")
    print(f"wrote {rec_path}\nwrote {rows_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
