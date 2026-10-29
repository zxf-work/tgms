"""Tests for the refresh-cost attribution record (lane SHRINK, P-SHRINK,
2026-10-29): ``scripts/refresh_cost_attribution_record.py`` and the committed
files it derives (``benchmarks/storm-v1/refresh-cost-attribution-2026-10-29*``).

The builder's arithmetic is checked on tiny hand-made fixtures; the committed
record is checked to be exactly what the builder derives from its own rows,
to carry the pre-registration verbatim and the outcome the README states, to
validate against the result-manifest schema, and to hold its checksum lines.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load("refresh_cost_attribution_record")


def _call(n, sample, op, total, execution, publication, rnd=None, name="storm-000001"):
    return {"row_type": "profile_call", "n_target": n, "n_registered": 3, "sample": sample,
            "round": rnd, "name": name, "op": op, "total_ms": total,
            "phases_ms": {"exec": execution, "publish": publication, "exec.kernel": execution - 1}}


def _batch_input(survivors, refresh_ms, global_ttf, l1_ttf, index=0):
    return {"batch_index": index, "n_registered": 10, "candidate_survivors": survivors,
            "arms": {"global-recompute": {"ttf_ms": global_ttf},
                     "tgms-L1": {"refresh_wall_ms": refresh_ms, "ttf_ms": l1_ttf}}}


# --------------------------------------------------------------------------
# builder arithmetic on tiny fixtures
# --------------------------------------------------------------------------

def test_phase_table_means_and_medians(mod):
    rows = [_call(1000, "same50", "compute", 10.0, 7.0, 3.0),
            _call(1000, "same50", "compute", 20.0, 16.0, 4.0),
            _call(1000, "same50", "compute", 60.0, 55.0, 5.0)]
    table = mod.phase_table(rows)
    assert table["total"] == {"mean": 30.0, "median": 20.0}
    assert table["execution"]["mean"] == pytest.approx(26.0)
    assert table["publication"] == {"mean": 4.0, "median": 4.0}
    assert table["kernel"]["mean"] == pytest.approx(25.0)
    assert table["fsync"]["mean"] == 0.0  # a phase a row does not carry counts as zero


def test_batch_row_speedup_and_expensive_flag(mod):
    cheap = mod._batch_row("src", "task-0", "cell", "sum", _batch_input(100, 400.0, 900.0, 300.0))
    assert cheap["refresh_ms_per_refreshed_artifact"] == pytest.approx(4.0)
    assert cheap["speedup"] == pytest.approx(3.0)
    assert cheap["expensive"] is False
    dear = mod._batch_row("src", "task-0", "cell", "sum", _batch_input(100, 2000.0, 900.0, 800.0))
    assert dear["refresh_ms_per_refreshed_artifact"] == pytest.approx(20.0)
    assert dear["expensive"] is True
    edge = mod._batch_row("src", "task-0", "cell", "sum", _batch_input(100, 1500.0, 1.0, 1.0))
    assert edge["expensive"] is False, "exactly 15 ms is not above the threshold"
    assert cheap["temporal_paths_share_of_refreshed"] is None


def test_cell_summary_ranges_and_modes(mod):
    rows = [mod._batch_row("s", "t", "c", "sum", _batch_input(10, ms, 100.0, l1, i))
            for i, (ms, l1) in enumerate([(40.0, 50.0), (400.0, 20.0), (50.0, 25.0)])]
    s = mod._cell_summary(rows)
    assert s["batches"] == 3
    assert s["speedup_min"] == pytest.approx(2.0) and s["speedup_max"] == pytest.approx(5.0)
    assert s["expensive_batches"] == 1
    assert s["expensive_mean_ms"] == pytest.approx(40.0)
    assert s["cheaper_mean_ms"] == pytest.approx(4.5)
    assert s["refresh_ms_pooled"] == pytest.approx(490.0 / 30)


def test_histogram_and_rows_from_profile(mod):
    assert mod.histogram({"a": "x", "b": "y", "c": "x"}) == {"x": 2, "y": 1}
    prof = {"rows": [{"tag": "s50.r2", "name": "n1", "op": "compute", "total": 1.5, "ph": {}},
                     {"tag": "strat", "name": "n2", "op": "compute", "total": 2.5, "ph": {}}]}
    out = mod.rows_from_profile(prof, 1000, 884)
    assert [(r["sample"], r["round"]) for r in out] == [("same50", 2), ("stratified", None)]
    assert all(r["n_target"] == 1000 and r["n_registered"] == 884 for r in out)


def test_result_digest_is_over_the_rows_in_order(mod):
    rows = [{"b": 1}, {"a": 2}]
    expect = hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert mod.result_digest(rows) == expect
    assert mod.result_digest(list(reversed(rows))) != expect


# --------------------------------------------------------------------------
# the committed record
# --------------------------------------------------------------------------

def test_committed_record_is_what_the_builder_derives(mod):
    rows = mod.load_jsonl(mod.ROWS)
    prof = [r for r in rows if r["row_type"] == "profile_call"]
    got_rows, manifest = mod.build(prof)
    assert got_rows == rows
    assert mod.dump_manifest(manifest) == mod.MANIFEST.read_text(encoding="utf-8")


def test_record_states_the_outcome_and_the_preregistration_verbatim(mod):
    m = json.loads(mod.MANIFEST.read_text(encoding="utf-8"))
    assert m["preregistration"] == mod.PREREGISTRATION
    assert m["preregistration"].startswith("P-SHRINK, frozen 2026-10-29T12:26:44Z.")
    assert m["verdict"]["prediction"].startswith("not supported")
    assert m["verdict"]["falsifier"].startswith("not triggered")
    assert "batch-composition artifact" in m["verdict"]["premise"]
    readme = (ROOT / "benchmarks" / "storm-v1" / "README.md").read_text(encoding="utf-8")
    section = re.search(r"^## Refresh cost does not grow with N \(2026-10-29\).*?(?=^## )", readme,
                        re.DOTALL | re.MULTILINE)
    assert section is not None
    text = section.group(0)
    assert "> " + m["preregistration"] in text
    assert "prediction not supported, falsifier not triggered" in text
    assert "batch-composition artifact of comparing medians over 5 batches" in text
    assert "_store_identity" in text


def test_phase_means_do_not_grow_with_n(mod):
    m = json.loads(mod.MANIFEST.read_text(encoding="utf-8"))
    ratios = m["summary"]["phase_means_ms"]["same50"]["ratio_n10000_over_n1000"]
    for phase in ("total", "execution", "publication", "append", "fsync"):
        assert 0.9 < ratios[phase] < 1.1, (phase, ratios[phase])
    assert m["summary"]["registry"]["n10000"]["n_entries"] == 26766
    assert m["summary"]["registry"]["n1000"]["n_entries"] == 2652


def test_committed_batches_come_from_the_committed_records(mod):
    rows = [r for r in mod.load_jsonl(mod.ROWS) if r["row_type"] == "committed_batch"]
    assert len(rows) == 365
    assert sum(r["cell"].endswith("-probe") for r in rows) == 5
    assert all(r["temporal_paths_share_of_refreshed"] is None for r in rows)
    assert mod.committed_rows() == rows


def test_manifest_validates_against_the_result_schema(mod):
    checker = _load("check_result_manifest")
    ok, message = checker.validate_one(mod.MANIFEST, checker.load_schema())
    assert ok, message


def test_checksum_lines_hold(mod):
    text = mod.SUMS.read_text(encoding="utf-8")
    entries = re.findall(r"^([0-9a-f]{64})  (\S+)$", text, re.MULTILINE)
    assert len(entries) == len(text.splitlines()) >= 20
    names = {p for _, p in entries}
    assert mod.MANIFEST.name in names and mod.ROWS.name in names
    for digest, rel in entries:
        assert hashlib.sha256((mod.STORM / rel).read_bytes()).hexdigest() == digest, rel
    on_disk = {str(p.relative_to(mod.STORM)) for p in mod.RAW.rglob("*") if p.is_file()}
    assert on_disk <= names, "every as-run input is checksummed"
