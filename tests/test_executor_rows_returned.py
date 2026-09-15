"""[tests] Executor.run's trace field `rows_returned` must record the
delivered page count, matching the ECQR scope's `rows_returned`
(`tgms/evidence/adapter_tgms.py`) -- not the engine's full-result count
`rows_total`, which the materialized-row budget keeps charging.

New test for a semantic fix, no ground-truth changes.
"""

from __future__ import annotations

import tgms
from tgms.agent.executor import Executor, ResultStore
from tgms.agent.ir import Plan
from tgms.tools.server import ToolRouter

T_MAX = 1000

#: one source fanning out to six distinct leaves at increasing timestamps,
#: so `temporal_reachability` from the source over the full window reaches
#: exactly six nodes (the op excludes the source itself)
_LEAVES = 6


def _plan(limit: int) -> Plan:
    return Plan.from_json({
        "plan_id": "rr-1",
        "steps": [
            {"id": "s1", "op": "resolve_entities", "args": {"query": "src0"},
             "depends_on": []},
            {"id": "s2", "op": "temporal_reachability",
             "args": {"src": {"$ref": "s1.rows[0].uid"},
                      "window": {"t_a": 0, "t_b": T_MAX}, "limit": limit},
             "depends_on": ["s1"]},
        ],
        "answer_spec": {"kind": "entity_set", "from": "s1.rows"},
    })


def _store(tmp_path):
    store = tgms.open(tmp_path / "store")
    store.ingest_events([
        {"src": "src0", "dst": f"leaf{i}", "rel_type": "MSG", "vt_s": 10 * (i + 1)}
        for i in range(_LEAVES)
    ])
    return store


def test_rows_returned_is_the_delivered_page(tmp_path):
    store = _store(tmp_path)
    results = ResultStore(tmp_path / "results")
    trace = Executor(ToolRouter(store.adapter), result_store=results).run(_plan(limit=2))

    by_id = {s["step_id"]: s for s in trace.steps}
    rec = by_id["s2"]
    assert rec["status"] == "ok"
    assert rec["truncated"] is True
    assert rec["rows_returned"] == 2

    stored = results.get(rec["result_digest"])
    assert rec["rows_returned"] == len(stored["rows"])
    # rows_total is the engine's full-result count -- larger than the page,
    # and > the limit we asked for
    assert stored["rows_total"] == _LEAVES
    assert stored["rows_total"] > 2

    # the trace field now agrees with the ECQR descriptor's scope, instead
    # of contradicting it
    assert rec["ecqr"]["scope"]["rows_returned"] == 2


def test_row_budget_still_charges_rows_total(tmp_path):
    """A budget that fits the *delivered pages* but not the *full results*
    still trips E_LIMIT -- the trace field's redefinition to the delivered
    page must not change what the materialized-row budget counts (it stays
    pinned to `rows_total`, summed cumulatively across steps)."""
    store = _store(tmp_path)
    results = ResultStore(tmp_path / "results")
    ex = Executor(ToolRouter(store.adapter), result_store=results, max_total_rows=5)
    trace = ex.run(_plan(limit=2))

    by_id = {s["step_id"]: s for s in trace.steps}
    s1, s2 = by_id["s1"], by_id["s2"]
    assert s1["status"] == "ok"
    s1_rows_total = results.get(s1["result_digest"])["rows_total"]
    # the budget is a running sum over rows_total across all prior steps;
    # a page-sized budget (well under s1's + s2's *delivered* rows, 1 + 2)
    # would pass, but the full-result sum (s1_rows_total + 6) exceeds it
    expected_total = s1_rows_total + _LEAVES
    assert expected_total > 5, "fixture assumption: budget=5 must be tripped"

    assert s2["status"] == "failed"
    assert s2["error"]["error"] == "E_LIMIT"
    assert f"materialized rows {expected_total} > 5" in s2["error"]["message"]
