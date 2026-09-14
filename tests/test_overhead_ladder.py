"""The five-rung overhead ladder harness (`scripts/bench_overhead_ladder.py`,
OSDI'27 lane D4). tmp_path-only: every store here is built fresh under
pytest's tmp_path, never copied from `stores/` (disk-tight laptop rule).

What is tested is the harness's own contract, not any reported number:
- the record it writes validates against `result_manifest.schema.json`;
- all five rungs are present in one run;
- rung 3 (trace bytes) is positive and reproducible across two independent
  runs with the same seed (see `_strip_wall_ms` in the harness -- wall-clock
  timing is stripped before the byte count, precisely so this holds);
- rung 5 (tool calls) counts exactly on a plan with two steps that both
  execute;
- each (rung, plan) condition the orchestrator drives runs in a fresh
  interpreter (a distinct pid), never in-process.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "benchmarks" / "schema" / "result_manifest.schema.json"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ladder = _load("bench_overhead_ladder")


# --------------------------------------------------------------------------- #
# fixtures: a tiny on-disk store + a two-step agent-IR plan, tmp_path only    #
# --------------------------------------------------------------------------- #

def _build_store(tmp_path: Path) -> Path:
    import tgms
    from tgms.core.model import OPEN_END

    store_path = tmp_path / "store"
    store = tgms.open(str(store_path))
    try:
        store.assert_node("n1", "Person", {"name": "Alice"}, vt_s=0, vt_e=OPEN_END)
        store.assert_node("n2", "Person", {"name": "Bob"}, vt_s=0, vt_e=OPEN_END)
        store.assert_edge("n1", "n2", "KNOWS", {}, vt_s=0, vt_e=OPEN_END)
    finally:
        store.close()
    return store_path


def _write_plan(tmp_path: Path, plan_id: str = "t1") -> Path:
    """Exactly two steps that both execute (s2 depends on s1) -- the "known
    2-call plan" rung 5's tool-call count is checked against."""
    plan = {
        "plan_id": plan_id,
        "question": "How many versions does n1 have?",
        "steps": [
            {"id": "s1", "op": "entity_history", "args": {"uid": "n1", "limit": 5},
             "depends_on": []},
            {"id": "s2", "op": "compute",
             "args": {"fn": "count", "input": {"$ref": "s1.rows"}},
             "depends_on": ["s1"]},
        ],
        "answer_spec": {"kind": "count", "from": "s2.value"},
    }
    path = tmp_path / f"{plan_id}.json"
    path.write_text(json.dumps(plan))
    return path


@pytest.fixture
def store_and_plan(tmp_path):
    store_path = _build_store(tmp_path)
    plan_path = _write_plan(tmp_path)
    return store_path, plan_path


# --------------------------------------------------------------------------- #
# schema conformance + all five rungs present, end to end via --smoke        #
# --------------------------------------------------------------------------- #

def test_smoke_record_validates_against_result_manifest_schema(tmp_path):
    out = tmp_path / "record.json"
    # invoke main() directly with a patched argv, in-process (the harness's
    # own subprocess children are exercised by every other test in this
    # file; this one only needs the orchestrator + manifest assembly)
    old_argv = sys.argv
    sys.argv = ["bench_overhead_ladder.py", "--smoke", "--out", str(out)]
    try:
        assert ladder.main() == 0
    finally:
        sys.argv = old_argv

    data = json.loads(out.read_text())
    schema = json.loads(SCHEMA_PATH.read_text())
    jsonschema.validate(data, schema)  # raises on failure


def test_smoke_run_covers_all_five_rungs(tmp_path):
    out = tmp_path / "record.json"
    old_argv = sys.argv
    sys.argv = ["bench_overhead_ladder.py", "--smoke", "--out", str(out)]
    try:
        assert ladder.main() == 0
    finally:
        sys.argv = old_argv

    data = json.loads(out.read_text())
    rungs = sorted(r["rung"] for r in data["rows"])
    assert rungs == [1, 2, 3, 4, 5]
    for row in data["rows"]:
        assert row.get("outcome", "OK") == "OK", row


# --------------------------------------------------------------------------- #
# rung 3: trace bytes positive and deterministic across two runs             #
# --------------------------------------------------------------------------- #

def test_rung3_trace_bytes_positive_and_deterministic(store_and_plan):
    store_path, plan_path = store_and_plan
    plan = ladder.load_plan(plan_path)

    r1 = ladder.measure_rung3(str(store_path), plan, reps=1)
    r2 = ladder.measure_rung3(str(store_path), plan, reps=1)

    assert r1["bytes_median"] > 0
    assert r1["bytes_median"] == r2["bytes_median"], (
        "trace byte count must be reproducible across runs with the same "
        "seed/store/plan -- wall_ms must be stripped before counting bytes")


def test_rung3_bytes_stable_across_reps_within_one_run(store_and_plan):
    store_path, plan_path = store_and_plan
    plan = ladder.load_plan(plan_path)

    result = ladder.measure_rung3(str(store_path), plan, reps=5)
    assert result["bytes_min"] == result["bytes_max"] == result["bytes_median"]
    assert len(result["bytes_all"]) == 5


# --------------------------------------------------------------------------- #
# rung 5: exact tool-call count on a known 2-call plan                       #
# --------------------------------------------------------------------------- #

def test_rung5_counts_tool_calls_exactly(store_and_plan):
    store_path, plan_path = store_and_plan
    plan = ladder.load_plan(plan_path)

    result = ladder.measure_rung5(str(store_path), plan, tokenizer="approx")
    assert result["tool_calls"] == 2
    assert result["n_steps"] == 2
    assert result["tokens"]["approx"] is True
    assert result["tokens"]["tokenizer"] == "approx:whitespace_punct"
    assert result["tokens"]["total"] > 0


def test_rung5_tool_call_count_matches_router_not_step_count(tmp_path):
    """A plan whose second step is skipped (its dependency fails) must not
    count the skipped step as a tool call -- the count is instrumented at
    `ToolRouter.call` itself, not read off `len(trace.steps)`."""
    store_path = _build_store(tmp_path)
    plan = {
        "plan_id": "t2",
        "steps": [
            {"id": "s1", "op": "entity_history",
             "args": {"uid": "does-not-exist", "limit": 5}, "depends_on": []},
            {"id": "s2", "op": "compute",
             "args": {"fn": "count", "input": {"$ref": "s1.rows"}},
             "depends_on": ["s1"]},
        ],
        "answer_spec": {"kind": "count", "from": "s2.value"},
    }
    plan_path = tmp_path / "t2.json"
    plan_path.write_text(json.dumps(plan))

    p = ladder.load_plan(plan_path)
    result = ladder.measure_rung5(str(store_path), p, tokenizer="approx")
    # s1 fails (E_NOT_FOUND), s2 is skipped (upstream failed) and never
    # reaches the router -- exactly one real tool call was made.
    assert result["tool_calls"] == 1
    assert result["n_steps"] == 2


# --------------------------------------------------------------------------- #
# rung 4: verify ms is measured over a real ClaimVerifier.verify() call      #
# --------------------------------------------------------------------------- #

def test_rung4_verify_ms_reports_a_real_verification(store_and_plan):
    store_path, plan_path = store_and_plan
    plan = ladder.load_plan(plan_path)

    result = ladder.measure_rung4(str(store_path), plan, reps=3)
    assert result["reps"] == 3
    assert result["min_ms"] >= 0.0
    assert result["n_claims"] == 1
    assert result["verify_metrics"] is not None


# --------------------------------------------------------------------------- #
# rung 1 / 2: reused-by-import functions cover the plan's operators         #
# --------------------------------------------------------------------------- #

def test_rung1_measures_the_plans_own_operators(store_and_plan):
    store_path, plan_path = store_and_plan
    plan = ladder.load_plan(plan_path)

    result = ladder.measure_rung1(str(store_path), plan)
    assert set(result["ops_measured"]) == {"entity_history", "compute"}
    for row in result["rows"]:
        assert row["leaf"]["outcome"] == "OK"
        assert row["direct"]["outcome"] == "OK"


def test_rung2_population_is_exactly_the_compiled_operators(store_and_plan):
    store_path, plan_path = store_and_plan
    plan = ladder.load_plan(plan_path)

    result = ladder.measure_rung2(str(store_path), plan)
    # the plan's ops are {entity_history, compute}; only entity_history is
    # in tgms.tgir.compiled.COMPILED, so rung 2 must measure exactly that
    # one op and skip `compute` -- not fail on it.
    assert result["ops_measured"] == ["entity_history"]
    assert result["rows"][0]["kernel"]["outcome"] == "OK"
    assert result["rows"][0]["compiled"]["outcome"] == "OK"


# --------------------------------------------------------------------------- #
# child-process isolation: each (rung, plan) condition is a fresh interpreter #
# --------------------------------------------------------------------------- #

def test_each_condition_runs_in_a_fresh_interpreter(store_and_plan):
    import os

    store_path, plan_path = store_and_plan
    row3 = ladder.run_condition(3, plan_path, str(store_path), reps=1, seed=0,
                                tokenizer="approx")
    row5 = ladder.run_condition(5, plan_path, str(store_path), reps=1, seed=0,
                                tokenizer="approx")
    assert row3["outcome"] == "OK" if "outcome" in row3 else True
    assert row3["pid"] != os.getpid()
    assert row5["pid"] != os.getpid()
    assert row3["pid"] != row5["pid"]


def test_orchestrator_spawns_one_process_per_rung_times_plan(tmp_path):
    """`main()`'s own row set, run end to end, carries a distinct pid per
    (rung, plan) row -- confirming the orchestrator never measures two
    conditions in the same interpreter."""
    store_path = _build_store(tmp_path)
    plan_a = _write_plan(tmp_path, "a")
    out = tmp_path / "record.json"

    old_argv = sys.argv
    sys.argv = ["bench_overhead_ladder.py", "--store", str(store_path),
               "--plans", str(plan_a), "--rungs", "3,4,5", "--reps", "1",
               "--seed", "0", "--tokenizer", "approx", "--out", str(out)]
    try:
        assert ladder.main() == 0
    finally:
        sys.argv = old_argv

    data = json.loads(out.read_text())
    pids = [r["pid"] for r in data["rows"]]
    assert len(pids) == 3
    assert len(set(pids)) == 3, "each condition must run in its own process"


# --------------------------------------------------------------------------- #
# tokenizer: the "approx" flag is honest about the fallback                  #
# --------------------------------------------------------------------------- #

def test_approx_tokenizer_is_labelled_and_tiktoken_is_not():
    n, label, approx = ladder.count_tokens("hello, world!", tokenizer="approx")
    assert approx is True
    assert label == "approx:whitespace_punct"
    assert n > 0

    tiktoken = pytest.importorskip("tiktoken")
    del tiktoken
    n2, label2, approx2 = ladder.count_tokens("hello, world!", tokenizer="tiktoken")
    assert approx2 is False
    assert label2.startswith("tiktoken:")
    assert n2 > 0


def test_unknown_tokenizer_choice_raises():
    with pytest.raises(ValueError):
        ladder.count_tokens("hi", tokenizer="not-a-real-tokenizer")


# --------------------------------------------------------------------------- #
# plan loading: directory vs comma-separated list                            #
# --------------------------------------------------------------------------- #

def test_plans_arg_accepts_a_directory(tmp_path):
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    _write_plan(plans_dir, "p1")
    _write_plan(plans_dir, "p2")
    found = ladder._iter_plan_paths(str(plans_dir))
    assert [p.stem for p in found] == ["p1", "p2"]


def test_plans_arg_accepts_a_comma_separated_list(tmp_path):
    p1 = _write_plan(tmp_path, "p1")
    p2 = _write_plan(tmp_path, "p2")
    found = ladder._iter_plan_paths(f"{p1},{p2}")
    assert found == [p1, p2]
