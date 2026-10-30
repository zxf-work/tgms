"""`scripts/axes_standing_state_record.py`: the P-AXES axis A record
assembler. Pins the per-cell arithmetic, the population check (both sides
must hold the same registered names), and that a failed input refuses."""

from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "axes_standing_state_record", ROOT / "scripts" / "axes_standing_state_record.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["axes_standing_state_record"] = mod
    spec.loader.exec_module(mod)
    return mod


def _view(cell_id: str, n: int, rss_kb: int) -> dict:
    labels = ("after_bundle_load", "after_load_and_registration", "after_burst_1", "end")
    cps = [{"label": lab, "vm_rss_kb": 90_000 if i == 0 else rss_kb,
            "vm_hwm_kb": 130_000 if i == 0 else rss_kb,
            "arranged_records": None if i == 0 else 1000 * n + i,
            "arrangements_nonempty": None if i == 0 else 89,
            "arrangements_seen": None if i == 0 else 92,
            "batcher_records": None if i == 0 else 0,
            "batcher_size_bytes": None if i == 0 else 0,
            "batcher_capacity_bytes": None if i == 0 else 0} for i, lab in enumerate(labels)]
    return {"returncode": 0, "interval_s": 1.0, "wall_s": 3.0, "sampled_rss_kb_max": rss_kb,
            "samples": [{"t_s": 1.0, "vm_rss_kb": rss_kb, "vm_hwm_kb": rss_kb}],
            "state_report": {"cell_id": cell_id, "artifacts": n, "bursts": 20, "workers": 1,
                             "load_ms": 10.0, "checkpoints": cps}}


def _tgms(n: int, ttf: str, base_kb: int, per_art_kb: float) -> dict:
    reg_kb = base_kb + int(per_art_kb * n)
    return {
        "seed": 0, "mix": "c1", "measure_ttf": ttf, "check_cache": "chain",
        "names_registered_sha256": f"names-{n}", "check_process_returncode": 0,
        "check_process_samples": [{"t_s": 1.0, "vm_rss_kb": reg_kb, "vm_hwm_kb": reg_kb}],
        "registration": {"n_artifacts_requested": 10000 if n > 5000 else 1000,
                         "n_registered": n, "registry_record_lines": n,
                         "fold": {"names": n, "entries": n},
                         "registry_bytes": 48 + 1400 * n},
        "burst_1": {"registry_bytes": 48 + 3000 * n},
        "harness_process": {"after_registration": {"vm_rss_kb": reg_kb + 40_000},
                            "after_burst_1": {"vm_rss_kb": reg_kb + 60_000}},
        "check_process": {
            "after_imports": {"vm_rss_kb": base_kb, "vm_hwm_kb": base_kb},
            "after_registry_load": {"vm_rss_kb": reg_kb, "vm_hwm_kb": reg_kb},
            "after_check_pass": {"vm_rss_kb": reg_kb + 15_000, "vm_hwm_kb": reg_kb + 30_000},
            "fold": {"names": n, "entries": n}, "registry_load_s": 0.1,
            "candidate_survivors": 10, "checks": 20, "not_fresh": {"L0": 10, "L1": 10},
            "chain_cache": {"hits": 19, "misses": 1}, "check_pass_s": 1.0},
    }


def _inputs():
    views = {"collegemsg-c1-none-n1000-s0": _view("collegemsg-c1-none-n1000-s0", 934, 570_000),
             "synth-iv-60k-c1-none-n1000-s0": _view("synth-iv-60k-c1-none-n1000-s0", 884,
                                                    4_270_000),
             "synth-iv-60k-c1-none-n10000-s0": _view("synth-iv-60k-c1-none-n10000-s0", 8922,
                                                     33_500_000)}
    tgms = {"collegemsg-c1-none-n1000-s0": _tgms(934, "end-to-end", 38_000, 4.8),
            "synth-iv-60k-c1-none-n1000-s0": _tgms(884, "end-to-end", 38_000, 4.8),
            "synth-iv-60k-c1-none-n10000-s0": _tgms(8922, "sum", 38_000, 4.8)}
    names = {"collegemsg-c1-none-n1000-s0": "names-934",
             "synth-iv-60k-c1-none-n1000-s0": "names-884",
             "synth-iv-60k-c1-none-n10000-s0": "names-8922"}
    return views, tgms, names


def _build(mod, views, tgms, names):
    return mod.build(views, tgms, names, git_commit="0" * 40,
                     timestamp_utc="2026-10-30T00:00:00Z",
                     host_block={"host": "h", "platform": "p", "cpus": 1, "ram_gb": 1.0},
                     date="2026-10-30", rows_relpath="r.jsonl", provenance={"x": 1},
                     dataset_digests={"a": "b"})


def test_build_computes_the_ratios_and_readings():
    mod = _load()
    record, rows = _build(mod, *_inputs())
    assert len(rows) == 6
    assert [r["configuration"] for r in rows] == ["ivm-dd", "tgms"] * 3
    s = record["summary"]
    probe = s["per_cell"]["synth-iv-60k-c1-none-n10000-s0"]
    t_chk = 38_000 + int(4.8 * 8922) + 15_000
    assert probe["ratio_view_over_tgms_after_burst_1"] == pytest.approx(33_500_000 / t_chk)
    assert probe["tgms_registry_bytes_per_artifact"] == pytest.approx((48 + 1400 * 8922) / 8922)
    assert probe["tgms_registry_mem_bytes_per_artifact"] == pytest.approx(
        int(4.8 * 8922) * 1024 / 8922)
    assert s["ratio_view_over_tgms_n10k_after_burst_1"] == probe["ratio_view_over_tgms_after_burst_1"]
    assert s["falsifier"]["triggered"] is False
    # 33.5 GB / 4.27 GB < 10.09x the artifacts: sub-linear
    assert s["predictions"]["view_grows_at_least_linearly"]["holds"] is False
    assert record["config"]["campaign"] == "axes-standing-state"
    assert record["preregistration"].startswith("P-AXES, frozen 2026-10-30T00:50:31Z")


def test_build_refuses_a_population_mismatch():
    mod = _load()
    views, tgms, names = _inputs()
    names = dict(names, **{"synth-iv-60k-c1-none-n1000-s0": "other"})
    with pytest.raises(ValueError, match="registered names differ"):
        _build(mod, views, tgms, names)
    views, tgms, names = _inputs()
    views = copy.deepcopy(views)
    views["collegemsg-c1-none-n1000-s0"]["state_report"]["artifacts"] = 933
    with pytest.raises(ValueError, match="view holds 933 artifacts"):
        _build(mod, views, tgms, names)


def test_build_refuses_a_failed_input():
    mod = _load()
    views, tgms, names = _inputs()
    views["synth-iv-60k-c1-none-n10000-s0"]["returncode"] = 1
    with pytest.raises(ValueError, match="ivm-dd exited"):
        _build(mod, views, tgms, names)
    views, tgms, names = _inputs()
    tgms["collegemsg-c1-none-n1000-s0"]["check_process_returncode"] = 1
    with pytest.raises(ValueError, match="check process failed"):
        _build(mod, views, tgms, names)
