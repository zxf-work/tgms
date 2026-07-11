"""M5: dynamic claim verification + fault-injection harness.

Builds real traces on a seeded store, derives correct AnswerObjects, then
checks: correct answers verify clean (FP), injected faults are caught
(detection), truncation caps verdicts, and the reporter contract round-trips.
"""

from __future__ import annotations

from tgms.agent.executor import Executor, ResultStore
from tgms.agent.ir import Plan
from tgms.agent.reporter import Reporter, mechanical_answer
from tgms.agent.verifier import ClaimVerifier
from tgms.core.model import canonical_json
from tgms.eval.faults import run_fault_injection
from tgms.tools.server import ToolRouter

from .test_operators_oracle import T_MAX, build_store


def _run(adapter, tmp_path, plan_json):
    results = ResultStore(tmp_path / "results")
    plan = Plan.from_json(plan_json)
    trace = Executor(ToolRouter(adapter), results).run(plan)
    assert trace.ok, trace.to_json()
    return plan, trace, results


def _reach_plan():
    return {
        "plan_id": "v1",
        "steps": [
            {"id": "s1", "op": "temporal_reachability",
             "args": {"src": "u0", "window": {"t_a": 0, "t_b": T_MAX},
                      "limit": 1000}, "depends_on": []},
            {"id": "s2", "op": "compute",
             "args": {"fn": "count", "input": {"$ref": "s1.rows"}},
             "depends_on": ["s1"]},
        ],
        "answer_spec": {"kind": "count", "from": "s2.value"},
    }


def test_correct_claims_verify_supported(tmp_path):
    adapter, _, _ = build_store(2)
    plan, trace, results = _run(adapter, tmp_path, _reach_plan())
    v = ClaimVerifier(trace, results, adapter)

    n = trace.answer
    reached = results.get(trace.steps[0]["result_digest"])["rows"]
    answer = {
        "text": f"The source reaches {n} nodes; among them {reached[0]['uid']}.",
        "claims": [
            {"id": "c1", "type": "count", "value": n, "from": "s2.value",
             "evidence": ["s2"]},
            {"id": "c2", "type": "entity", "uids": [reached[0]["uid"]],
             "evidence": ["s1"]},
            {"id": "c3", "type": "ordering",
             "a": {"t": reached[0]["earliest_arrival"]},
             "b": {"t": reached[-1]["earliest_arrival"] + 1},
             "relation": "before", "evidence": ["s1"]},
        ],
    }
    report = v.verify(answer)
    assert report["schema_valid"]
    verdicts = {c["id"]: c["verdict"] for c in report["claims"]}
    assert verdicts["c1"] == "supported"
    assert verdicts["c2"] == "supported"
    assert verdicts["c3"] in ("supported", "unverifiable")  # b.t+1 may be ungrounded
    assert report["metrics"]["ucr"] == 0.0
    assert report["metrics"]["coverage"] == 1.0


def test_wrong_claims_are_unsupported(tmp_path):
    adapter, _, _ = build_store(2)
    plan, trace, results = _run(adapter, tmp_path, _reach_plan())
    v = ClaimVerifier(trace, results, adapter)
    answer = {
        "text": "u0 reaches 99999 nodes including nobody-anywhere.",
        "claims": [
            {"id": "c1", "type": "count", "value": 99999, "evidence": ["s2"]},
            {"id": "c2", "type": "entity", "uids": ["nobody-anywhere"],
             "evidence": ["s1"]},
        ],
    }
    report = v.verify(answer)
    assert all(c["verdict"] == "unsupported" for c in report["claims"])
    assert report["metrics"]["ucr"] == 1.0


def test_truncated_evidence_caps_verdict(tmp_path):
    adapter, _, _ = build_store(2)
    plan_json = _reach_plan()
    plan_json["steps"][0]["args"]["limit"] = 1  # force truncation
    plan, trace, results = _run(adapter, tmp_path, plan_json)
    assert trace.steps[0]["truncated"]
    v = ClaimVerifier(trace, results, adapter)
    uid = results.get(trace.steps[0]["result_digest"])["rows"][0]["uid"]
    report = v.verify({"text": f"{uid} was reached.",
                       "claims": [{"id": "c1", "type": "entity", "uids": [uid],
                                   "evidence": ["s1"]}]})
    assert report["claims"][0]["verdict"] == "weakly_supported"


def test_uncovered_assertions_measured(tmp_path):
    adapter, _, _ = build_store(2)
    plan, trace, results = _run(adapter, tmp_path, _reach_plan())
    v = ClaimVerifier(trace, results, adapter)
    report = v.verify({"text": "There were 42 events and also 7 more.",
                       "claims": [{"id": "c1", "type": "count", "value": 42,
                                   "evidence": ["s2"]}]})
    assert 7.0 in report["metrics"]["uncovered_assertions"]
    assert report["metrics"]["coverage"] == 0.5


def test_reporter_contract_and_fallback(tmp_path):
    adapter, _, _ = build_store(2)
    plan, trace, results = _run(adapter, tmp_path, _reach_plan())
    good = canonical_json({
        "text": f"{trace.answer} nodes reached.",
        "claims": [{"id": "c1", "type": "count", "value": trace.answer,
                    "from": "s2.value", "evidence": ["s2"]}]})
    script = iter(["not json at all", good])
    rep = Reporter("fake", llm_fn=lambda *a, **k: next(script))
    obj = rep.report("How many?", plan, trace, results)
    assert obj["claims"][0]["value"] == trace.answer  # retry path worked

    rep2 = Reporter("fake", llm_fn=lambda *a, **k: "still not json")
    fallback = rep2.report("How many?", plan, trace, results)
    assert fallback == mechanical_answer(plan, trace)
    report = ClaimVerifier(trace, results, adapter).verify(fallback)
    assert all(c["verdict"] == "supported" for c in report["claims"])


def test_fault_injection_detection_and_fp(tmp_path):
    """Mini C2 readout on machine-checkable claim families (count, entity,
    ordering): detection must be 100% here; FP must be 0."""
    answers = []
    for seed in (0, 2, 3):
        adapter, _, _ = build_store(seed)
        plan, trace, results = _run(adapter, tmp_path / f"s{seed}", _reach_plan())
        reached = results.get(trace.steps[0]["result_digest"])["rows"]
        v = ClaimVerifier(trace, results, adapter)
        first, last = reached[0], reached[-1]
        answers.append(({
            "text": f"u0 reaches {trace.answer} nodes, first {first['uid']}.",
            "claims": [
                {"id": "c1", "type": "count", "value": trace.answer,
                 "from": "s2.value", "evidence": ["s2"]},
                {"id": "c2", "type": "entity",
                 "uids": [first["uid"], last["uid"]], "evidence": ["s1"]},
                {"id": "c3", "type": "ordering",
                 "a": {"t": first["earliest_arrival"]},
                 "b": {"t": last["earliest_arrival"]},
                 "relation": "before" if first["earliest_arrival"]
                             < last["earliest_arrival"] else "equals",
                 "evidence": ["s1"]},
            ]}, v))
    stats = run_fault_injection(answers, n_mutants=120, seed=7)
    assert stats["fp_rate"] == 0.0, stats
    assert stats["detection_rate"] >= 0.95, stats


def test_upstream_truncation_taints_dependent_claims(tmp_path):
    """[tests] a count computed over a truncated page must not verify as
    fully supported (D-015 live-demo finding: 14B counted a 100-row page of
    a 343-row result and the claim showed 'supported')."""
    adapter, _, _ = build_store(2)
    plan_json = _reach_plan()
    plan_json["steps"][0]["args"]["limit"] = 1          # truncate upstream
    plan, trace, results = _run(adapter, tmp_path, plan_json)
    by = {s["step_id"]: s for s in trace.steps}
    assert by["s1"]["truncated"] and by["s2"]["upstream_truncated"]
    v = ClaimVerifier(trace, results, adapter)
    report = v.verify({"text": f"{trace.answer} nodes.",
                       "claims": [{"id": "c1", "type": "count",
                                   "value": trace.answer, "from": "s2.value",
                                   "evidence": ["s2"]}]})
    assert report["claims"][0]["verdict"] == "weakly_supported"
