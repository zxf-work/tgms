"""[tests] The certified rendering path (Option A): deterministic text
from pre-verified claims only, schema-clean output, honest abstention.

New tests for new machinery, no ground-truth changes.
"""

from __future__ import annotations

import json

import jsonschema
import pytest

import tgms
from tgms.agent.executor import Executor, ResultStore
from tgms.agent.ir import Plan
from tgms.agent.reporter import certified_answer
from tgms.agent.verifier import ANSWER_SCHEMA, ClaimVerifier
from tgms.tools.server import ToolRouter


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    from tgms.data.synth import generate
    tmp = tmp_path_factory.mktemp("cert")
    generate(tmp / "synth", n_nodes=50, n_events=800, seed=5)
    store = tgms.open(tmp / "store")
    with open(tmp / "synth" / "events.jsonl") as f:
        store.ingest_events(json.loads(line) for line in f if line.strip())
    stats = store.stats()
    plan = Plan.from_json({
        "plan_id": "cert-1", "question": "q",
        "steps": [
            {"id": "s1", "op": "graph_metric_timeseries",
             "args": {"metric": "edge_event_count",
                      "window": {"t_a": stats["vt_min"],
                                 "t_b": stats["vt_max"] + 2},
                      "stride": stats["vt_max"] + 2 - stats["vt_min"]},
             "depends_on": []},
        ],
        "answer_spec": {"kind": "value", "from": "s1.rows[0].value"}})
    results = ResultStore(tmp / "results")
    trace = Executor(ToolRouter(store.adapter), result_store=results).run(plan)
    return {"store": store, "plan": plan, "trace": trace, "results": results}


def test_certified_path_renders_only_verified_claims(env):
    out = certified_answer(env["plan"], env["trace"], env["results"])
    ao, cert = out["answer_object"], out["certification"]
    jsonschema.validate(ao, ANSWER_SCHEMA)
    assert cert["certified_rendering"] is True
    assert cert["claims"][0]["certified"] is True
    assert "[c1]" in ao["text"]
    assert str(env["trace"].answer) in ao["text"]
    # the certified object flows through the legacy gate unchanged
    report = ClaimVerifier(env["trace"], env["results"],
                           adapter=env["store"].adapter).verify(ao)
    assert report["claims"][0]["verdict"] in ("supported", "weakly_supported")


def test_uncertifiable_claim_becomes_abstention(env):
    # a backend without descriptors (or a stripped trace) must abstain,
    # never assert
    import copy
    bare = copy.deepcopy(env["trace"])
    for s in bare.steps:
        s.pop("ecqr", None)
    out = certified_answer(env["plan"], bare, env["results"])
    ao, cert = out["answer_object"], out["certification"]
    jsonschema.validate(ao, ANSWER_SCHEMA)
    assert cert["claims"][0]["certified"] is False
    assert ao["claims"] == []
    assert "could not be certified" in ao["text"]
    assert str(env["trace"].answer) + "." not in ao["text"]


def test_rendering_is_deterministic(env):
    a = certified_answer(env["plan"], env["trace"], env["results"])
    b = certified_answer(env["plan"], env["trace"], env["results"])
    assert a == b
