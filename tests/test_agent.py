"""M4: Plan IR ($ref language), O13 compute, static validation, executor
determinism, planner repair loop (fake LLM — no network)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tgms.agent.executor import Executor, ResultStore
from tgms.agent.ir import Plan, parse_ref, resolve_ref, substitute_refs
from tgms.agent.planner import Planner, strip_fences
from tgms.agent.verifier import validate_static
from tgms.core.errors import InvalidArgError
from tgms.core.model import canonical_json
from tgms.temporal.algebra import call_operator, ensure_all_registered
from tgms.temporal.ops_compute import allen_relation
from tgms.tools.server import ToolRouter

from .test_operators_oracle import T_MAX, build_store

ensure_all_registered()
FEWSHOT = sorted((Path(__file__).parents[1] / "configs" / "fewshot").glob("*.json"))


# --------------------------- $ref language ---------------------------------- #

def test_parse_and_resolve_refs():
    out = {"rows": [{"uid": "a", "n": 1}, {"uid": "b", "n": 2}], "count": 7,
           "nested": {"x": [10, 20]}}
    assert resolve_ref(parse_ref("s1.count"), out) == 7
    assert resolve_ref(parse_ref("s1.rows[0].uid"), out) == "a"
    assert resolve_ref(parse_ref("s1.rows[*].uid"), out) == ["a", "b"]
    assert resolve_ref(parse_ref("s1.nested.x[1]"), out) == 20
    for bad in ("nostep.count", "s1", "s1.rows[*].uid[*]", "s1.rows[x]"):
        with pytest.raises(InvalidArgError):
            resolve_ref(parse_ref(bad), out) if "." in bad else parse_ref(bad)


def test_substitute_refs_truncates_projections():
    outputs = {"s1": {"rows": [{"uid": f"u{i}"} for i in range(10)]}}
    trunc: list = []
    args = {"node_filter": {"$ref": "s1.rows[*].uid"},
            "keep": [1, {"$ref": "s1.rows[0].uid"}]}
    resolved = substitute_refs(args, outputs, trunc, max_items=4)
    assert resolved["node_filter"] == ["u0", "u1", "u2", "u3"]
    assert resolved["keep"] == [1, "u0"]
    assert trunc == [{"ref": "s1.rows[*].uid", "from": 10, "to": 4}]


# --------------------------- O13 compute ------------------------------------ #

def test_compute_functions():
    adapter, _, _ = build_store(0)
    rows = [{"uid": "a", "v": 3}, {"uid": "b", "v": 1}, {"uid": "c", "v": 2}]

    def c(args):
        return call_operator(adapter, "compute", args)

    assert c({"fn": "count", "input": rows})["value"] == 3
    assert c({"fn": "sum", "input": rows, "field": "v"})["value"] == 6
    assert c({"fn": "min", "input": rows, "field": "v"})["value"] == 1
    assert c({"fn": "max", "input": rows, "field": "v"})["value"] == 3
    top = c({"fn": "topk", "input": rows, "field": "v", "k": 2})
    assert [r["uid"] for r in top["rows"]] == ["a", "c"]
    kept = c({"fn": "filter", "input": rows, "field": "v", "cmp": "ge", "value": 2})
    assert [r["uid"] for r in kept["rows"]] == ["a", "c"]
    rel = c({"fn": "interval_relation", "a": {"start": 0, "end": 5},
             "b": {"start": 5, "end": 9}})
    assert rel["value"] == "meets"


def test_allen_relation_classification_is_total_and_consistent():
    cases = {
        ((0, 2), (5, 9)): "before", ((0, 5), (5, 9)): "meets",
        ((0, 6), (5, 9)): "overlaps", ((5, 9), (0, 6)): "overlapped_by",
        ((6, 8), (5, 9)): "during", ((5, 9), (6, 8)): "contains",
        ((5, 7), (5, 9)): "starts", ((5, 9), (5, 7)): "started_by",
        ((7, 9), (5, 9)): "finishes", ((5, 9), (7, 9)): "finished_by",
        ((5, 9), (5, 9)): "equals", ((5, 9), (0, 2)): "after",
        ((5, 9), (0, 5)): "met_by",
    }
    for (a, b), expected in cases.items():
        got = allen_relation({"start": a[0], "end": a[1]},
                             {"start": b[0], "end": b[1]})
        assert got == expected, f"{a} vs {b}: {got} != {expected}"


# --------------------------- static validation ------------------------------ #

def test_fewshot_exemplars_are_statically_valid():
    assert len(FEWSHOT) == 4
    for p in FEWSHOT:
        plan = json.loads(p.read_text())["plan"]
        verdict = validate_static(plan)
        assert verdict["valid"], (p.name, verdict["violations"])


def _mini_plan(**over):
    base = {
        "plan_id": "t1",
        "steps": [
            {"id": "s1", "op": "resolve_entities", "args": {"query": "u1"},
             "depends_on": []},
            {"id": "s2", "op": "entity_history",
             "args": {"uid": {"$ref": "s1.rows[0].uid"}}, "depends_on": ["s1"]},
        ],
        "answer_spec": {"kind": "series", "from": "s2.rows"},
    }
    base.update(over)
    return base


def test_static_violations_detected():
    ok = validate_static(_mini_plan())
    assert ok["valid"]

    cyc = _mini_plan()
    cyc["steps"][0]["depends_on"] = ["s2"]
    assert not validate_static(cyc)["valid"]

    scope = _mini_plan()
    scope["steps"][1]["depends_on"] = []
    v = validate_static(scope)
    assert any("depends_on" in x["message"] for x in v["violations"])

    unknown = _mini_plan()
    unknown["steps"][0]["op"] = "made_up_op"
    assert any(x["code"] == "E_NOT_FOUND"
               for x in validate_static(unknown)["violations"])

    grounding = _mini_plan()
    grounding["steps"][1]["args"] = {"uid": "fabricated-uid-42"}
    v = validate_static(grounding, task_input_uids={"u1"})
    assert any(x["code"] == "E_GROUNDING" for x in v["violations"])
    # ...but task-input uids are fine
    grounded = _mini_plan()
    grounded["steps"][1]["args"] = {"uid": "u1"}
    assert validate_static(grounded, task_input_uids={"u1"})["valid"]

    badwin = _mini_plan()
    badwin["steps"][1]["args"] = {"uid": {"$ref": "s1.rows[0].uid"},
                                  "as_of_tt": -5}
    assert not validate_static(badwin)["valid"]


# --------------------------- executor ---------------------------------------- #

def _exec_plan():
    return Plan.from_json({
        "plan_id": "e2e-1",
        "steps": [
            {"id": "s1", "op": "resolve_entities", "args": {"query": "u0"},
             "depends_on": []},
            {"id": "s2", "op": "temporal_reachability",
             "args": {"src": {"$ref": "s1.rows[0].uid"},
                      "window": {"t_a": 0, "t_b": T_MAX}, "limit": 1000},
             "depends_on": ["s1"]},
            {"id": "s3", "op": "count_temporal_motifs",
             "args": {"motif": "M_path_3", "delta": 20,
                      "window": {"t_a": 0, "t_b": T_MAX},
                      "node_filter": {"$ref": "s2.rows[*].uid"}},
             "depends_on": ["s2"]},
            {"id": "s4", "op": "compute",
             "args": {"fn": "count", "input": {"$ref": "s2.rows"}},
             "depends_on": ["s2"]},
        ],
        "answer_spec": {"kind": "count", "from": "s3.count"},
    })


def test_executor_runs_dag_and_roundtrips(tmp_path):
    adapter, _, _ = build_store(3)
    ex = Executor(ToolRouter(adapter), ResultStore(tmp_path / "results"))
    t1 = ex.run(_exec_plan())
    assert t1.ok and isinstance(t1.answer, int)
    digests1 = [s["result_digest"] for s in t1.steps]
    # M4 acceptance: re-execution reproduces identical digests
    t2 = ex.run(_exec_plan())
    assert [s["result_digest"] for s in t2.steps] == digests1
    # full results retrievable from the content-addressed store
    stored = ResultStore(tmp_path / "results").get(digests1[0])
    assert stored["op"] == "resolve_entities"


def test_executor_failure_isolation():
    adapter, _, _ = build_store(3)
    plan = Plan.from_json({
        "plan_id": "fail-1",
        "steps": [
            {"id": "s1", "op": "entity_history", "args": {"uid": "no-such-uid"},
             "depends_on": []},
            {"id": "s2", "op": "compute",
             "args": {"fn": "count", "input": {"$ref": "s1.rows"}},
             "depends_on": ["s1"]},
            {"id": "s3", "op": "resolve_entities", "args": {"query": "u1"},
             "depends_on": []},
        ],
        "answer_spec": {"kind": "count", "from": "s2.value"},
    })
    t = Executor(ToolRouter(adapter)).run(plan)
    by = {s["step_id"]: s for s in t.steps}
    assert by["s1"]["status"] == "failed"
    assert by["s1"]["error"]["error"] == "E_NOT_FOUND"
    assert by["s2"]["status"] == "skipped"        # dependent fails
    assert by["s3"]["status"] == "ok"             # independent branch runs
    assert t.answer_error is not None


# --------------------------- planner (fake LLM) ------------------------------ #

def test_planner_repair_loop_and_cache(tmp_path):
    adapter, _, _ = build_store(3)
    good = canonical_json(_exec_plan().to_json())
    bad_json = "this is not json"
    bad_plan = canonical_json({"plan_id": "x", "steps": [
        {"id": "s1", "op": "made_up", "args": {}}],
        "answer_spec": {"kind": "count", "from": "s1.count"}})
    script = [bad_json, bad_plan, good]
    calls = {"n": 0}

    def fake_llm(model, messages, temperature, seed):
        out = script[min(calls["n"], len(script) - 1)]
        calls["n"] += 1
        return out

    planner = Planner(model="fake", tool_manual="(manual)", llm_fn=fake_llm,
                      cache_dir=tmp_path / "cache", max_repairs=3)
    res = planner.plan("q?", {"extent": {}}, adapter=adapter)
    assert res.plan is not None
    assert not res.first_emission_valid            # PVR datum: first try invalid
    assert len(res.attempts) == 3
    assert res.attempts[0].error["error"] == "E_SCHEMA"
    assert res.attempts[1].error["error"] == "E_PLAN_INVALID"

    # cache: an identical first prompt is served without an LLM call
    planner2 = Planner(model="fake", tool_manual="(manual)", llm_fn=fake_llm,
                       cache_dir=tmp_path / "cache", max_repairs=3)
    n_before = calls["n"]
    res2 = planner2.plan("q?", {"extent": {}}, adapter=adapter)
    assert res2.attempts[0].raw == bad_json        # same first response, cached
    assert res2.plan is not None
    assert calls["n"] == n_before                  # deterministic rerun: free


def test_agent_ask_end_to_end_with_runtime_repair(tmp_path):
    import tgms
    from tgms.agent.agent import Agent

    store = tgms.open(tmp_path / "agent-store")
    store.ingest_events([
        {"src": "alice", "dst": "bob", "rel_type": "MSG", "vt_s": 10},
        {"src": "bob", "dst": "carol", "rel_type": "MSG", "vt_s": 20},
    ])

    # first plan queries a uid that does not exist -> runtime E_NOT_FOUND;
    # the repaired plan resolves the entity first
    broken = canonical_json({
        "plan_id": "p1", "steps": [
            {"id": "s1", "op": "entity_history", "args": {"uid": "alicia"},
             "depends_on": []}],
        "answer_spec": {"kind": "series", "from": "s1.rows"}})
    fixed = canonical_json({
        "plan_id": "p2", "steps": [
            {"id": "s1", "op": "resolve_entities", "args": {"query": "alice"},
             "depends_on": []},
            {"id": "s2", "op": "temporal_reachability",
             "args": {"src": {"$ref": "s1.rows[0].uid"},
                      "window": {"t_a": 0, "t_b": 100}},
             "depends_on": ["s1"]},
            {"id": "s3", "op": "compute",
             "args": {"fn": "count", "input": {"$ref": "s2.rows"}},
             "depends_on": ["s2"]}],
        "answer_spec": {"kind": "count", "from": "s3.value"}})
    script = iter([broken, fixed])

    agent = Agent(store, model="fake",
                  llm_fn=lambda *a, **k: next(script))
    out = agent.ask("How many nodes can alice reach?")
    assert out["answer"] == 2                      # bob, carol
    assert out["trace"].ok
    assert len(out["plan_result"].attempts) == 2   # broken + repaired
    store.close()


def test_strip_fences():
    assert strip_fences('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert strip_fences('{"a": 1}') == '{"a": 1}'
