"""`scripts/axes_on_demand_record.py`: the P-AXES axis B assembler. Pins the
crossover interpolation and its two edge cases, the per-k medians, the
first-check reading, the verdict, and that a run with the wrong flags,
code or read counts refuses."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SHA = "f" * 40


def _load():
    spec = importlib.util.spec_from_file_location(
        "axes_on_demand_record", ROOT / "scripts" / "axes_on_demand_record.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["axes_on_demand_record"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_crossover_interpolates_and_names_its_edges():
    mod = _load()
    x = mod.crossover([1, 10, 100], [5.0, 50.0, 500.0], 95.0)
    assert x["kind"] == "interpolated" and x["between"] == [10, 100]
    assert x["k_star"] == pytest.approx(10 + 45 * 90 / 450)
    assert mod.crossover([1, 10], [7.0, 70.0], 6.0)["kind"] == "none"
    assert mod.crossover([1, 10], [7.0, 70.0], 7.0)["kind"] == "none"
    assert mod.crossover([1, 10], [1.0, 2.0], 6.0)["kind"] == "above_grid"


def _run(cell_id, store, n, batches, ttf, k, n_reg, per_read, first, *, flags=None):
    cfg = {"refresh_on_read": k, "n_artifacts": n, "seed": 0, "mix": "c1",
           "check_cache": "chain", "measure_ttf": ttf, "backend": "native",
           "burst_size": 10000, "burst_after": 10, "batches": batches, "wall_s": 100.0,
           "wall_capped": False}
    cfg.update(flags or {})
    reads = min(k, n_reg)
    rows = []
    for b in range(batches):
        refreshes = 0 if (k == 1 and b % 2) else max(1, reads // 4)
        rows.append({"batch_index": b, "n_registered": n_reg, "candidate_survivors": 10,
                     "changed_count": 3, "on_demand_k": k,
                     "on_demand_ms": first + per_read * (reads - 1) + 2.0 * refreshes + b * 0.01,
                     "on_demand_reads": reads, "on_demand_refreshes": refreshes,
                     "on_demand_refresh_errors": 0, "on_demand_check_cache_misses": 1,
                     "check_cache_misses": 1, "ttf_mode": ttf, "correction_class": "A",
                     "correction_generator": "g",
                     "arms": {"tgms-L1": {"invalidated_count": 10, "false_fresh_count": 0}}})
    manifest = {"git_commit": SHA, "config": cfg, "timestamp_utc": "2026-10-30T03:00:00Z",
                "result_digest": "d", "dataset": {"digest": f"log-{store}"}}
    return manifest, rows, {"manifest_sha256": "m", "rows_sha256": "r"}


def _inputs(mod, **over):
    regs = {"collegemsg-c1-none-n1000-s0": 934, "synth-iv-60k-c1-none-n1000-s0": 884,
            "synth-iv-60k-c1-none-n10000-s0": 8922}
    runs = {}
    for cell_id, store, n, batches, ttf, grid in mod.CELLS:
        for k in grid:
            runs[(cell_id, k)] = _run(cell_id, store, n, batches, ttf, k, regs[cell_id],
                                      0.25, 5.0, **over)
    view_rows = [
        {"cell_id": "collegemsg-c1-none-n1000-s0",
         "result": {"per_burst": [{"refresh_plus_publish_ms": v} for v in (2.0, 6.0, 60.0)]}},
        {"cell_id": "synth-iv-60k-c1-none-n1000-s0",
         "result": {"per_burst": [{"refresh_plus_publish_ms": v} for v in (50.0, 113.0, 400.0)]}},
        {"cell_id": "synth-iv-60k-c1-none-n10000-s0",
         "result": {"per_burst": [{"refresh_plus_publish_ms": v} for v in (5000.0, 5400.0)]}},
    ]
    return runs, view_rows


def _build(mod, runs, view_rows):
    return mod.build(runs, view_rows, {"before": {}}, git_commit=SHA,
                     timestamp_utc="2026-10-30T04:00:00Z",
                     host_block={"host": "h", "platform": "p", "cpus": 1, "ram_gb": 1.0},
                     date="2026-10-30", rows_relpath="r.jsonl", provenance={},
                     view_rows_sha256="v")


def test_build_scores_the_crossover_against_the_view_median_and_mean():
    mod = _load()
    record, rows = _build(mod, *_inputs(mod))
    assert len(rows) == 4 * 20 + 4 * 20 + 5 * 5
    pc = record["summary"]["per_cell"]
    s1k = pc["synth-iv-60k-c1-none-n1000-s0"]
    assert s1k["view_per_burst"]["median_ms"] == 113.0
    assert s1k["per_k"]["1000"]["k_effective"] == 884
    assert s1k["crossover_median"]["kind"] == "interpolated"
    assert s1k["crossover_mean"]["kind"] in ("interpolated", "above_grid")
    # collegemsg: TGMS at k = 1 (5-7 ms) vs a 6 ms median view
    col = pc["collegemsg-c1-none-n1000-s0"]
    assert col["crossover_median"]["kind"] in ("none", "interpolated")
    # the k = 1 batches without a refresh are the first-check reading
    assert record["summary"]["first_check_ms_median_all_cells"] == pytest.approx(5.0, abs=0.2)
    v = record["summary"]["verdict"]
    assert v["falsifier_k_star_below_1_on_synth"] is False
    assert isinstance(v["k_star_synth_n10k"], float)
    assert record["config"]["campaign"] == "axes-on-demand"
    assert record["preregistration"].startswith("P-AXES, frozen 2026-10-30T00:50:31Z")
    assert record["addendum"].startswith("Addendum AXES-A")


@pytest.mark.parametrize("flags,match", [
    ({"check_cache": "none"}, "committed flags"),
    ({"mix": "c3"}, "committed flags"),
    ({"refresh_on_read": 7}, "config"),
    ({"wall_capped": True}, "wall-capped"),
])
def test_build_refuses_a_run_off_protocol(flags, match):
    mod = _load()
    runs, view_rows = _inputs(mod, flags=flags)
    with pytest.raises(ValueError, match=match):
        _build(mod, runs, view_rows)


def test_build_refuses_other_code_or_short_reads():
    mod = _load()
    runs, view_rows = _inputs(mod)
    key = ("synth-iv-60k-c1-none-n1000-s0", 10)
    runs[key][0]["git_commit"] = "e" * 40
    with pytest.raises(ValueError, match="git_commit"):
        _build(mod, runs, view_rows)
    runs, view_rows = _inputs(mod)
    runs[key][1][3]["on_demand_reads"] = 9
    with pytest.raises(ValueError, match="reads at batch 3"):
        _build(mod, runs, view_rows)
