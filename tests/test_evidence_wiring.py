"""[tests] ECQR wiring: every ok step carries a descriptor, inheritance
runs over dependency edges, and the legacy gate gains the observational
ecqr_verdict column without its own verdicts moving.

New tests for new wiring, no ground-truth changes.
"""

from __future__ import annotations

import json

import pytest

import tgms
from tgms.agent.executor import Executor, ResultStore
from tgms.agent.ir import Plan
from tgms.agent.verifier import ClaimVerifier
from tgms.tools.server import ToolRouter


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    from tgms.data.synth import generate
    tmp = tmp_path_factory.mktemp("wire")
    generate(tmp / "synth", n_nodes=50, n_events=800, seed=5)
    store = tgms.open(tmp / "store")
    with open(tmp / "synth" / "events.jsonl") as f:
        store.ingest_events(json.loads(line) for line in f if line.strip())
    stats = store.stats()
    plan = Plan.from_json({
        "plan_id": "wire-1", "question": "q",
        "steps": [
            {"id": "s1", "op": "graph_metric_timeseries",
             "args": {"metric": "edge_event_count",
                      "window": {"t_a": stats["vt_min"],
                                 "t_b": stats["vt_max"] + 2},
                      "stride": stats["vt_max"] + 2 - stats["vt_min"]},
             "depends_on": []},
            {"id": "s2", "op": "compute",
             "args": {"fn": "count", "input": {"$ref": "s1.rows"}},
             "depends_on": ["s1"]},
        ],
        "answer_spec": {"kind": "value", "from": "s1.rows[0].value"}})
    results = ResultStore(tmp / "results")
    trace = Executor(ToolRouter(store.adapter), result_store=results).run(plan)
    return {"store": store, "trace": trace, "results": results}


def test_ok_steps_carry_descriptors_with_inheritance(env):
    trace = env["trace"]
    assert trace.ok
    by_id = {s["step_id"]: s for s in trace.steps}
    e1, e2 = by_id["s1"]["ecqr"], by_id["s2"]["ecqr"]
    assert e1 and e2
    assert e1["schema"] == "ECQR"
    assert e1["scope"]["execution_complete"] is True
    # the dependent step's provenance names its input's result id
    assert e2["provenance"]["inputs"] == [e1["result_id"]]


def test_legacy_gate_gains_observational_column(env):
    trace = env["trace"]
    value = trace.answer
    answer = {"text": f"There were {value} events.",
              "claims": [{"id": "c1", "type": "value", "value": value,
                          "evidence": ["s1"], "from": "s1.rows[0].value"}]}
    report = ClaimVerifier(trace, env["results"],
                           adapter=env["store"].adapter).verify(answer)
    assert report["schema_valid"]
    c = report["claims"][0]
    assert c["verdict"] in ("supported", "weakly_supported")
    assert c.get("ecqr_verdict") == "SUPPORTED"


def test_wrong_value_disagrees_in_both_columns(env):
    trace = env["trace"]
    answer = {"text": "There were 999999 events.",
              "claims": [{"id": "c1", "type": "value", "value": 999999,
                          "evidence": ["s1"], "from": "s1.rows[0].value"}]}
    report = ClaimVerifier(trace, env["results"],
                           adapter=env["store"].adapter).verify(answer)
    c = report["claims"][0]
    assert c["verdict"] == "unsupported"
    assert c.get("ecqr_verdict", "").startswith("UNSUPPORTED")
