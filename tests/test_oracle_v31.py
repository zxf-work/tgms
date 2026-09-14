"""[tests] Oracle-v3.1 labeling contract (D-116).

New tests for new machinery, plus one justified ground-truth change in
test_oracle_v3.py (not_attempted replaces the v3 mislabel). What is
pinned here: gold_source on every record; the budget_exceeded relabel
covering every oracle-envelope cap; the empty-result rule firing only on
engine-established emptiness (complete, untruncated, zero rows) and its
resolutions staying OUT of the LLM suites; the declared oracle envelope;
and the eligibility rule that makes v3 -> v3.1 split-hash invariant.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import tgms
from tgms.data.synth import generate
from tgms.eval.tasks import (EMPTY_GOLD, _budget_detail,
                             _empty_result_evidence, generate_suite)

SIZES = {"t1": 20, "t3": 9, "t4": 8, "probes": 4}

GOLD_SOURCES = {"production", "oracle", "empty_result_rule", "manifest",
                None}
STATUSES = {"resolved", "budget_exceeded", "oracle_unsupported",
            "not_attempted"}


@pytest.fixture(scope="module")
def v31_env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("v31")
    generate(tmp / "synth", n_nodes=200, n_events=4000, seed=3,
             n_rings=2, n_pingpong=1, n_bursts=1)
    store = tgms.open(tmp / "store")
    with open(tmp / "synth" / "events.jsonl") as f:
        store.ingest_events(json.loads(line) for line in f if line.strip())
    suite = generate_suite(store, "synth-v31", seed=1, sizes=SIZES)
    return {"store": store, "suite": suite}


# --------------------------------------------------------------------------- #
# record labeling                                                              #
# --------------------------------------------------------------------------- #

def test_schema_and_declared_envelope(v31_env):
    suite = v31_env["suite"]
    assert suite["schema"] == "oracle-v3.1"
    assert suite["oracle_envelope"] == {"budget_s": 120.0,
                                        "max_rows": 50_000}


def test_every_record_carries_gold_source(v31_env):
    for r in v31_env["suite"]["records"]:
        assert "gold_source" in r, r["task_id"]
        assert r["gold_source"] in GOLD_SOURCES, r["task_id"]
        if r["oracle_status"] == "resolved":
            assert r["gold_source"] is not None, r["task_id"]
        else:
            assert r["gold_source"] is None, r["task_id"]


def test_status_vocabulary(v31_env):
    for r in v31_env["suite"]["records"]:
        assert r["oracle_status"] in STATUSES, (
            f"{r['task_id']}: {r['oracle_status']}")


def test_rule_resolved_records_stay_out_of_suites(v31_env):
    # the split-hash invariance argument: only production/oracle golds are
    # eligible, exactly the v3 eligible set
    suite = v31_env["suite"]
    by_id = {r["task_id"]: r for r in suite["records"]}
    for t in suite["dev"] + suite["test"]:
        assert by_id[t["id"]]["gold_source"] in ("production", "oracle",
                                                 "manifest")
    for r in suite["records"]:
        if r["gold_source"] == "empty_result_rule":
            assert r["suite_eligible"] is False
            assert r["ineligible_reason"] == "empty_result"
            assert r["oracle_status"] == "resolved"


# --------------------------------------------------------------------------- #
# the empty-result rule: fires only on engine-established emptiness            #
# --------------------------------------------------------------------------- #

def _trace(step):
    return SimpleNamespace(steps=[step])

EMPTY_ERR = {"error": "E_ANSWER",
             "message": "$ref 's1.rows[0].uid': index 0 out of range "
                        "(0 rows) — the producing step returned no rows"}

def _step(status="ok", ex=True, dl=True, rows=0):
    return {"step_id": "s1", "status": status,
            "ecqr": {"result_id": "r1",
                     "scope": {"execution_complete": ex,
                               "delivery_complete": dl,
                               "rows_returned": rows}}}


def test_empty_rule_fires_on_complete_empty_producer():
    ev = _empty_result_evidence(_trace(_step()), EMPTY_ERR)
    assert ev is not None
    assert ev["producing_step"] == "s1"
    assert ev["scope"] == {"execution_complete": True,
                           "delivery_complete": True, "rows_returned": 0}


def test_empty_rule_refuses_truncated_producer():
    # a truncated empty page is a budget problem, not an empty window
    assert _empty_result_evidence(
        _trace(_step(dl=False)), EMPTY_ERR) is None
    assert _empty_result_evidence(
        _trace(_step(ex=False)), EMPTY_ERR) is None


def test_empty_rule_refuses_failed_or_nonempty_producer():
    assert _empty_result_evidence(
        _trace(_step(status="failed")), EMPTY_ERR) is None
    assert _empty_result_evidence(
        _trace(_step(rows=3)), EMPTY_ERR) is None
    assert _empty_result_evidence(
        _trace(_step()), {"error": "E_ANSWER",
                          "message": "something else"}) is None


def test_empty_gold_shapes():
    assert EMPTY_GOLD == {"entity_set": [], "count": 0, "value": None,
                          "interval": None}


# --------------------------------------------------------------------------- #
# budget_exceeded covers every oracle-envelope cap                             #
# --------------------------------------------------------------------------- #

def test_budget_detail_mapping():
    assert _budget_detail("timeout", None) == "wall_clock"
    assert _budget_detail("resource_exhausted", None) == "memory"
    # the kernel pair cap surfaces as E_COST with the admission gate off —
    # v3 mapped it to oracle_unsupported under an "unreachable" comment
    assert _budget_detail("cost_refused", {"error": "E_COST"}) == "kernel_cap"
    assert _budget_detail("failed", {
        "error": "E_LIMIT",
        "message": "materialized rows 74374 > 50000"}) == "row_cap"
    assert _budget_detail("failed", {
        "error": "E_LIMIT",
        "message": "compute count would reduce a truncated result to one "
                   "number, which is a wrong answer"}) == "row_cap"
    assert _budget_detail("failed", {
        "error": "E_ANSWER", "message": "no such field"}) is None


# --------------------------------------------------------------------------- #
# the oracle row cap is a declared parameter                                   #
# --------------------------------------------------------------------------- #

def test_oracle_max_rows_is_threaded(v31_env):
    tiny = generate_suite(v31_env["store"], "synth-v31", seed=1,
                          sizes={"t1": 2, "t3": 1, "t4": 1, "probes": 1},
                          oracle_max_rows=12_345)
    assert tiny["oracle_envelope"]["max_rows"] == 12_345
    oracle_receipts = [r["oracle_receipt"] for r in tiny["records"]
                       if (r.get("oracle_receipt") or {}).get("lane")
                       == "oracle"]
    for rec in oracle_receipts:
        assert rec["max_rows"] == 12_345
