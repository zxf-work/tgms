"""`scripts/axes_recompute_on_read_record.py`: the P-ROR assembler. Pins the
per-k statistics of both timer scopes, the crossovers and the regime gain for
both configurations, the apply-reference estimate on the axis B runs, the
prediction inputs (numbers beside thresholds, no verdict), and that a run with
the wrong flags, policy, code, read or refresh counts refuses."""

from __future__ import annotations

import importlib.util
import statistics
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SHA = "f" * 40
COL = "collegemsg-c1-none-n1000-s0"
S1K = "synth-iv-60k-c1-none-n1000-s0"
S10K = "synth-iv-60k-c1-none-n10000-s0"
REGS = {COL: 934, S1K: 884, S10K: 8922}


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _mods():
    axes = _load("axes_on_demand_record")
    ror = _load("axes_recompute_on_read_record")
    return axes, ror


def _manifest(cell, n, ttf, k, batches, policy, **flags):
    cfg = {"refresh_on_read": k, "refresh_on_read_policy": policy, "n_artifacts": n, "seed": 0,
           "mix": "c1", "check_cache": "chain", "measure_ttf": ttf, "backend": "native",
           "burst_size": 10000, "burst_after": 10, "batches": batches, "wall_s": 100.0,
           "wall_capped": False}
    cfg.update(flags)
    return {"git_commit": SHA, "config": cfg, "timestamp_utc": "2026-10-30T09:00:00Z",
            "result_digest": "d", "dataset": {"digest": f"log-{cell}"}}


def _rows(cell, ttf, k, batches, policy, first, per_read, apply_ms, refresh_ms):
    n_reg = REGS[cell]
    reads = min(k, n_reg)
    out = []
    for b in range(batches):
        if policy == "always":
            refreshes, ms = reads, refresh_ms * reads + b * 0.01
        else:
            refreshes = 0 if (k == 1 and b % 2) else max(1, reads // 4)
            ms = first + per_read * (reads - 1) + 2.0 * refreshes + b * 0.01
        r = {"batch_index": b, "n_registered": n_reg, "candidate_survivors": 10,
             "changed_count": 3, "on_demand_k": k, "on_demand_ms": ms,
             "on_demand_reads": reads, "on_demand_refreshes": refreshes,
             "on_demand_refresh_errors": 0,
             "on_demand_check_cache_misses": 0 if policy == "always" else 1,
             "check_cache_misses": 1, "correction_apply_ms": apply_ms + 0.1 * b,
             "ttf_mode": ttf, "correction_class": "A", "correction_generator": "g",
             "arms": {"tgms-L1": {"invalidated_count": 10, "false_fresh_count": 0}}}
        if policy == "always":
            r["on_demand_policy"] = "always"
        out.append(r)
    return out


VIEWS = [
    {"cell_id": COL, "result": {"per_burst": [{"refresh_plus_publish_ms": v}
                                             for v in (2.0, 6.0, 60.0)]}},
    {"cell_id": S1K, "result": {"per_burst": [{"refresh_plus_publish_ms": v}
                                             for v in (50.0, 113.0, 400.0)]}},
    {"cell_id": S10K, "result": {"per_burst": [{"refresh_plus_publish_ms": v}
                                              for v in (5000.0, 5400.0)]}},
]


def _inputs(axes, ror, *, always_flags=None):
    """Synthetic axis B record + rows (check policy) and this lane's runs."""
    axes_runs = {}
    for cell_id, store, n, batches, ttf, grid in axes.CELLS:
        for k in grid:
            man = _manifest(cell_id, n, ttf, k, batches, "check")
            del man["config"]["refresh_on_read_policy"]
            axes_runs[(cell_id, k)] = (man, _rows(cell_id, ttf, k, batches, "check", 5.0, 0.25,
                                                  3.0, 0.0), {"manifest_sha256": "m",
                                                              "rows_sha256": "r"})
    # axes.build wants `git_commit` equal to the manifest's
    axes_record, axes_rows = axes.build(
        axes_runs, VIEWS, {"before": {}}, git_commit=SHA, timestamp_utc="2026-10-30T04:00:00Z",
        host_block={"host": "h", "platform": "p", "cpus": 1, "ram_gb": 1.0}, date="2026-10-30",
        rows_relpath="r.jsonl", provenance={}, view_rows_sha256="v")
    runs = {}
    per_artifact = {COL: 3.4, S1K: 15.3, S10K: 15.65}
    for cell_id, store, n, batches, ttf, grid in axes.CELLS:
        for k in grid:
            for policy in ("always", "check"):
                if policy == "check" and (cell_id == S10K or k not in (grid[0], grid[-1])):
                    continue
                man = _manifest(cell_id, n, ttf, k, batches, policy,
                                **(always_flags or {}) if policy == "always" else {})
                runs[(cell_id, policy, k)] = (
                    man, _rows(cell_id, ttf, k, batches, policy, 5.0, 0.25, 3.0,
                               per_artifact[cell_id]),
                    {"manifest_sha256": "m", "rows_sha256": "r"})
    return runs, axes_record, axes_rows


def _build(ror, runs, axes_record, axes_rows):
    return ror.build(runs, axes_record, axes_rows, VIEWS, {"smoke": 1}, git_commit=SHA,
                     timestamp_utc="2026-10-30T10:00:00Z",
                     host_block={"host": "h", "platform": "p", "cpus": 1, "ram_gb": 1.0},
                     date="2026-10-30", rows_relpath="r.jsonl", provenance={"code": "c"},
                     input_shas={"axes_record": "a", "axes_rows": "b", "view_rows": "c"})


def test_both_scopes_and_both_policies_are_reported_by_median_and_mean():
    axes, ror = _mods()
    record, rows = _build(ror, *_inputs(axes, ror))
    # always: 4 + 4 + 5 grid points; check controls: 2 cells x 2 k
    assert len(rows) == (4 * 20 + 4 * 20 + 5 * 5) + 4 * 20
    assert {r["policy"] for r in rows} == {"always", "check"}
    s = record["summary"]["per_cell"][S1K]
    blk = s["per_k"]["1000"]["always"]
    batch = [3.0 + 0.1 * b for b in range(20)]
    ms = [15.3 * 884 + b * 0.01 for b in range(20)]
    assert blk["k_effective"] == 884 and blk["batches"] == 20
    assert blk["reads_only"]["median_ms"] == pytest.approx(statistics.median(ms))
    assert blk["reads_only"]["mean_ms"] == pytest.approx(statistics.fmean(ms))
    assert blk["with_apply"]["median_ms"] == pytest.approx(
        statistics.median([m + a for m, a in zip(ms, batch)]))
    assert blk["with_apply"]["mean_ms"] == pytest.approx(
        statistics.fmean([m + a for m, a in zip(ms, batch)]))
    assert blk["with_apply_kind"] == "measured"
    assert blk["refreshes"] == blk["reads"] == 20 * 884
    assert blk["median_ms_per_read"] == pytest.approx(15.3, rel=1e-3)
    # the same-day control carries the measured second scope, the record's runs the estimate
    ctrl = s["per_k"]["1000"]["check_control"]
    assert ctrl["with_apply_kind"] == "measured"
    assert "check_control" in s["per_k"]["1"] and "check_control" not in s["per_k"]["10"]
    rec = s["per_k"]["10"]["check_record"]
    assert rec["with_apply_kind"] == "estimate"
    assert rec["with_apply"]["median_ms"] > rec["reads_only"]["median_ms"]


def test_crossovers_and_regime_gain_for_both_configurations():
    axes, ror = _mods()
    record, _ = _build(ror, *_inputs(axes, ror))
    pc = record["summary"]["per_cell"]
    s = pc[S1K]
    x = s["crossover"]["reads_only"]
    # recompute on read: 15.3 ms x k against a 113 ms view median -> crossing between 1 and 10
    assert x["recompute_on_read"]["median"]["kind"] == "interpolated"
    assert x["recompute_on_read"]["median"]["between"] == [1, 10]
    k_ror = x["recompute_on_read"]["median"]["k_star"]
    assert 1 < k_ror < 10
    k_tgms = x["tgms_check"]["median"]["k_star"]
    assert x["regime_gain"]["median"]["value"] == pytest.approx(k_tgms / k_ror)
    assert x["regime_gain"]["median"]["kind"] == "ratio"
    # the with-apply scope has its own crossovers, and apply only ever adds cost
    xa = s["crossover"]["with_apply"]
    assert xa["recompute_on_read"]["median"]["k_star"] < k_ror
    # the TGMS crossover on the record's runs equals the axis B record's by construction
    assert s["per_read_at_n"]["ratio_recompute_to_tgms"] == pytest.approx(
        s["per_k"]["1000"]["always"]["median_ms_per_read"]
        / s["per_k"]["1000"]["check_record"]["median_ms_per_read"])


def test_a_crossover_that_does_not_exist_leaves_the_gain_undefined():
    _, ror = _mods()
    t = {"kind": "interpolated", "k_star": 20.0}
    assert ror._gain(t, {"kind": "interpolated", "k_star": 4.0})["value"] == 5.0
    g = ror._gain(t, {"kind": "none", "k_star": None})
    assert g["value"] is None and g["kind"] == "undefined"
    assert ror._gain({"kind": "above_grid", "k_star": None}, t)["value"] is None


def test_prediction_inputs_carry_numbers_and_thresholds_but_no_verdict():
    axes, ror = _mods()
    record, _ = _build(ror, *_inputs(axes, ror))
    pi = record["summary"]["prediction_inputs"]
    p1 = pi[S1K]["p1_recompute_ms_per_read_at_k_N"]
    assert p1["reference_per_artifact_refresh_ms"] == 15.3 and p1["band"] == [0.7, 1.3]
    assert p1["ratio_median_to_reference"] == pytest.approx(1.0, rel=1e-3)
    assert pi[COL]["p2_tgms_cheaper_per_read_at_k_N"]["min_ratio"] == 2.5
    p2 = pi[S1K]["p2_tgms_cheaper_per_read_at_k_N"]
    assert p2["recompute_over_tgms_ratio"] > 1 and p2["recompute_over_tgms_ratio_mean"] > 1
    assert pi[S10K]["p2_tgms_cheaper_per_read_at_k_N"]["min_ratio"] == 2.0
    assert pi[S1K]["p4_crossover_and_gain_median_reads_only"]["gain_bound"] == ["at_least", 2.5]
    assert pi[COL]["p4_crossover_and_gain_median_reads_only"]["gain_bound"] == ["below", 1.5]
    fals = pi[S1K]["falsifier_per_k_recompute_over_tgms_median_reads_only"]
    assert set(fals) == {"1", "10", "100", "1000"}
    flat = repr(record["summary"])
    assert "verdict" not in flat and "holds" not in flat
    assert record["preregistration"].startswith("P-ROR, frozen 2026-10-30T07:56:05Z")
    assert "Falsifier: TGMS on demand costs at least as much" in record["preregistration"]
    assert record["config"]["campaign"] == "axes-recompute-on-read"


@pytest.mark.parametrize("flags,match", [
    ({"check_cache": "none"}, "committed flags"),
    ({"mix": "c3"}, "committed flags"),
    ({"refresh_on_read": 7}, "config"),
    ({"wall_capped": True}, "wall-capped"),
    ({"refresh_on_read_policy": "check"}, "policy in the manifest"),
])
def test_build_refuses_a_run_off_protocol(flags, match):
    axes, ror = _mods()
    runs, axes_record, axes_rows = _inputs(axes, ror, always_flags=flags)
    with pytest.raises(ValueError, match=match):
        _build(ror, runs, axes_record, axes_rows)


def test_build_refuses_other_code_a_short_read_a_skipped_refresh_or_a_check_in_always():
    axes, ror = _mods()
    key = (S1K, "always", 10)
    runs, axes_record, axes_rows = _inputs(axes, ror)
    runs[key][0]["git_commit"] = "e" * 40
    with pytest.raises(ValueError, match="git_commit"):
        _build(ror, runs, axes_record, axes_rows)
    runs, axes_record, axes_rows = _inputs(axes, ror)
    runs[key][1][3]["on_demand_reads"] = 9
    with pytest.raises(ValueError, match="reads at batch 3"):
        _build(ror, runs, axes_record, axes_rows)
    runs, axes_record, axes_rows = _inputs(axes, ror)
    runs[key][1][3]["on_demand_refreshes"] = 9
    with pytest.raises(ValueError, match="every read refreshed at batch 3"):
        _build(ror, runs, axes_record, axes_rows)
    runs, axes_record, axes_rows = _inputs(axes, ror)
    runs[key][1][3]["on_demand_check_cache_misses"] = 1
    with pytest.raises(ValueError, match="no check at batch 3"):
        _build(ror, runs, axes_record, axes_rows)
    runs, axes_record, axes_rows = _inputs(axes, ror)
    del runs[key][1][3]["on_demand_policy"]
    with pytest.raises(ValueError, match="row policy at batch 3"):
        _build(ror, runs, axes_record, axes_rows)
    runs, axes_record, axes_rows = _inputs(axes, ror)
    runs[(S1K, "check", 1)][1][2]["correction_apply_ms"] = None
    with pytest.raises(ValueError, match="correction_apply_ms at batch 2"):
        _build(ror, runs, axes_record, axes_rows)


def test_build_refuses_a_missing_run_or_a_record_that_is_not_the_axis_b_rows():
    axes, ror = _mods()
    runs, axes_record, axes_rows = _inputs(axes, ror)
    del runs[(COL, "always", 100)]
    with pytest.raises(ValueError, match="missing run"):
        _build(ror, runs, axes_record, axes_rows)
    runs, axes_record, axes_rows = _inputs(axes, ror)
    del runs[(COL, "check", 1000)]
    with pytest.raises(ValueError, match="missing control"):
        _build(ror, runs, axes_record, axes_rows)
    runs, axes_record, axes_rows = _inputs(axes, ror)
    axes_rows[0]["on_demand_ms"] += 1.0
    with pytest.raises(ValueError, match="axis B rows"):
        _build(ror, runs, axes_record, axes_rows)
