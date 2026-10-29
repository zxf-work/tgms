"""Unit tests for `scripts/withheld_symmetric.py` (P-WH-SYM): the pure helpers
and the bundle loader, on a tiny hand-built bundle. The replay itself needs a
real store and engine and is exercised by the experiment run, not here."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "withheld_symmetric", ROOT / "scripts" / "withheld_symmetric.py")
ws = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("withheld_symmetric", ws)
assert _spec.loader is not None
_spec.loader.exec_module(ws)


def _oracle():
    # 12 artifacts a00..a11; epoch 10 changes a00..a02, epoch 11 changes a02..a05
    base = {f"a{i:02d}": f"d{i}" for i in range(12)}
    o = {e: dict(base) for e in range(0, 12)}
    for e in (10, 11):
        for n in ("a00", "a01", "a02"):
            o[e][n] = f"{n}-e10"
    for n in ("a02", "a03", "a04", "a05"):
        o[11][n] = f"{n}-e11"
    return o


def test_changed_names_is_the_per_epoch_oracle_difference():
    o = _oracle()
    assert ws.changed_names(o, 10) == ["a00", "a01", "a02"]
    assert ws.changed_names(o, 11) == ["a02", "a03", "a04", "a05"]
    assert ws.changed_names(o, 9) == []


def test_pick_control_is_deterministic_disjoint_and_sized():
    o = _oracle()
    c1 = ws.pick_control(o, 10, 5)
    c2 = ws.pick_control(o, 10, 5)
    assert c1 == c2 and len(c1) == 5
    assert not set(c1) & set(ws.changed_names(o, 10))
    with pytest.raises(ValueError):
        ws.pick_control(o, 10, 10)


def test_delayed_schedule_appends_the_held_burst_after_burst_11():
    ev = ws.schedule_events("delayed", 11)
    assert ev[:9] == [("write", e) for e in range(1, 10)]
    assert ev[9] == ("idle", 10)
    assert ev[10] == ("write", 11)
    assert ev[-1] == ("late", 10)
    assert ("write", 10) not in ev


def test_inorder_schedule_is_the_plain_sequence():
    assert ws.schedule_events("inorder", 11) == [("write", e) for e in range(1, 12)]
    with pytest.raises(ValueError):
        ws.schedule_events("sideways")


def test_row_flags_separate_log_relative_from_eventual_staleness():
    # fresh and equal to the log, but not equal to eventual truth: the
    # contract holds (not false-fresh relative to the log) and the answer is
    # stale relative to eventual truth.
    f = ws.row_flags(fresh=True, served="x", log_truth="x", oracle_truth="y")
    assert f == {"false_fresh_log": False, "stale_vs_eventual": True}
    # fresh but differing from the log: the contract violation
    f = ws.row_flags(fresh=True, served="x", log_truth="z", oracle_truth="x")
    assert f == {"false_fresh_log": True, "stale_vs_eventual": False}
    # a possibly-stale verdict is never a false-fresh of either kind
    f = ws.row_flags(fresh=False, served="x", log_truth="z", oracle_truth="y")
    assert f == {"false_fresh_log": False, "stale_vs_eventual": False}


def test_count_rows_counts_per_read_point_and_group():
    def row(rp, g, v0, v1, ffl=False, sve=False):
        return {"read_point": rp, "group": g, "verdict_l0": v0, "verdict_l1": v1,
                "false_fresh_log": ffl, "stale_vs_eventual": sve}
    rows = [row("R10", "affected", "fresh", "fresh", sve=True),
            row("R10", "affected", "fresh", "fresh", sve=True),
            row("R10", "control", "fresh", "fresh"),
            row("R11-after-arrival", "affected", "possibly-stale", "possibly-stale"),
            row("R11-after-arrival", "affected", "undecidable", "possibly-stale")]
    c = ws.count_rows(rows, "R10", "affected")
    assert (c["n"], c["fresh"], c["stale_vs_eventual"], c["false_fresh_log"]) == (2, 2, 2, 0)
    c = ws.count_rows(rows, "R11-after-arrival", "affected")
    assert (c["n"], c["fresh"], c["possibly_stale"], c["undecidable"]) == (2, 0, 2, 1)
    assert ws.count_rows(rows, "R10", "control")["n"] == 1


def test_load_bundle_orders_batches_by_epoch_and_checks_tt(tmp_path):
    (tmp_path / "artifacts.jsonl").write_text(
        json.dumps({"name": "storm-0", "op": "compute", "args": {}}) + "\n")
    (tmp_path / "eventlog-tail.jsonl").write_text(
        "".join(json.dumps({"batch_id": f"b{e}", "ops": [{"op": "x", "n": e}], "tt": 100 + e})
                + "\n" for e in (1, 2)))
    (tmp_path / "deltas.jsonl").write_text(
        "".join(json.dumps({"epoch": e, "tt": 100 + e}) + "\n" for e in (1, 2)))
    (tmp_path / "oracle.jsonl").write_text(
        "".join(json.dumps({"epoch": e, "digests": {"storm-0": f"d{e}"}, "refused": []}) + "\n"
                for e in (0, 1, 2)))
    b = ws.load_bundle(tmp_path)
    assert b["batches"][2] == {"tt": 102, "ops": [{"op": "x", "n": 2}]}
    assert b["oracle"][1] == {"storm-0": "d1"} and sorted(b["oracle"]) == [0, 1, 2]
    (tmp_path / "deltas.jsonl").write_text(
        "".join(json.dumps({"epoch": e, "tt": 7}) + "\n" for e in (1, 2)))
    with pytest.raises(ValueError):
        ws.load_bundle(tmp_path)
