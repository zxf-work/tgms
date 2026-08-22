"""M4.4 — the surface: `Store.check_*`, `check_trace`, `tgms trace check`.

The gate this file discharges (M4_IMPLEMENTATION_PLAN §6, M4.4 row):

(a) the CLI renders the memo §14 sentence **from a real witness** — not from a
    fixture, and not from a hand-written string;
(b) per-step attribution is present;
(c) the **merged-vs-per-step monotonicity invariant** holds: the merged check
    is never `FRESH` where the per-step fold is `POSSIBLY_STALE`;
(d) **no new envelope key**, and no comparator changes shape.

A verdict is computed on demand and persisted nowhere, which is what makes (d)
structural rather than a promise.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

import tgms
from tgms.agent.executor import Executor
from tgms.agent.ir import Plan
from tgms.cli import build_parser, main
from tgms.core.model import OPEN_END, EntityRef
from tgms.temporal.algebra import ENVELOPE_META_FIELDS
from tgms.tgir.check import ChainCache, StepsVerdict, Verdict
from tgms.tgir.depscope import DependencyScope, union_all
from tgms.tools.server import ToolRouter

PLAN = {
    "plan_id": "surface-1",
    "steps": [
        {"id": "s1", "op": "entity_history", "args": {"uid": "A", "include_edges": True}},
        {"id": "s2", "op": "entity_history", "args": {"uid": "B"}},
        {"id": "s3", "op": "compute", "args": {"fn": "count", "input": [{"x": 1}]}},
    ],
    "answer_spec": {"kind": "count", "from": "s1.rows"},
}


@pytest.fixture
def store(request):
    path = Path(tempfile.mkdtemp()) / "store"
    s = tgms.open(path, backend="duckdb")
    for uid in ("A", "B", "C"):
        s.assert_node(uid, "N", {"tier": "bronze"}, 0, 100)
    s.assert_edge("A", "B", "MSG", {}, 10, 20)
    yield s
    s.close()


def _run(store) -> dict[str, Any]:
    return Executor(ToolRouter(store.adapter, tt_source=store)).run(
        Plan.from_json(PLAN)).to_json()


# ---------------------------------------------------------------------------
# (a)/(b) the store API and per-step attribution
# ---------------------------------------------------------------------------

def test_a_result_envelope_can_ask_about_itself(store):
    env = ToolRouter(store.adapter, tt_source=store).call(
        "entity_history", {"uid": "A"})
    assert store.check_result(env).actionable_fresh
    store.assert_node("A", "N", {"tier": "gold"}, 0, 100)
    verdict = store.check_result(env)
    assert not verdict.actionable_fresh
    assert verdict.witnesses[0].identity == {"uid": "A"}


def test_an_unrelated_write_leaves_a_narrow_scope_fresh(store):
    """The precision claim, and the one that would be trivially satisfiable by
    a mechanism that answered `POSSIBLY_STALE` to everything."""
    env = ToolRouter(store.adapter, tt_source=store).call(
        "entity_history", {"uid": "A"})
    store.assert_node("C", "N", {"tier": "gold"}, 0, 100)
    assert store.check_result(env).actionable_fresh


def test_an_envelope_with_no_dependency_refuses_rather_than_certifying(store):
    """A result produced before M2.1 placed the key, or by a path that bypassed
    `envelope_metadata`. There is no basis to compare against."""
    assert store.check_result({}).reason == "no-tt_q"
    assert not store.check_result({"rows": []}).actionable_fresh


def test_check_scope_accepts_a_scope_object_or_its_json(store):
    env = ToolRouter(store.adapter, tt_source=store).call(
        "entity_history", {"uid": "A"})
    parsed = DependencyScope.from_json(env["dependency"])
    assert store.check_scope(parsed).to_json() == \
        store.check_scope(env["dependency"]).to_json()


def test_a_trace_record_is_checked_per_step_with_attribution(store):
    record = _run(store)
    assert store.check_trace(record).actionable_fresh

    store.assert_node("A", "N", {"tier": "gold"}, 0, 100)
    verdict = store.check_trace(record)
    assert isinstance(verdict, StepsVerdict)
    assert not verdict.actionable_fresh
    per = dict(verdict.per_step)
    assert not per["s1"].actionable_fresh
    assert per["s2"].actionable_fresh, "B was not touched"
    assert per["s3"].actionable_fresh, "compute carries the empty scope"
    assert {w.step_id for w in verdict.witnesses} == {"s1"}


def test_the_plan_bit_and_the_per_step_map_are_both_reported(store):
    """D5.4, §13.8.4. The headline is one bit; the map is what diagnoses which
    operator's scope is loose, and it is the number §4.6 disaggregates."""
    record = _run(store)
    store.assert_node("A", "N", {"tier": "gold"}, 0, 100)
    out = store.check_trace(record).to_json()
    assert out["verdict"] == "possibly-stale"
    assert set(out["steps"]) == {"s1", "s2", "s3"}
    assert out["steps"]["s2"]["verdict"] == "fresh"


def test_a_failed_step_still_contributes_its_scope(store):
    """D13.14: it read whatever it read before it failed, and dropping it would
    narrow the plan's dependency to the steps that happened to succeed."""
    plan = dict(PLAN)
    plan["steps"] = [{"id": "s1", "op": "entity_history",
                      "args": {"uid": "no-such-uid"}}]
    record = Executor(ToolRouter(store.adapter, tt_source=store)).run(
        Plan.from_json(plan)).to_json()
    assert record["steps"][0]["status"] == "failed"
    assert dict(store.check_trace(record).per_step).keys() == {"s1"}


# ---------------------------------------------------------------------------
# (c) the monotonicity invariant
# ---------------------------------------------------------------------------

def test_the_merged_check_is_never_fresh_where_the_per_step_fold_is_stale(store):
    """Monotonicity of the widening, and a cheap regression net.

    The merged scope (D13.8) forces the **earliest** `tt_q` onto every term —
    the widening FF-7 required for a single scope object, and one that is not
    required while the steps are still separate. So merging can only ever add
    witnesses, never remove them.
    """
    record = _run(store)
    for write in (lambda: store.assert_node("A", "N", {"t": 2}, 0, 100),
                  lambda: store.assert_node("B", "N", {"t": 2}, 0, 100),
                  lambda: store.assert_edge("A", "B", "MSG", {"w": 1}, 10, 20),
                  lambda: store.assert_node("C", "N", {"t": 2}, 0, 100),
                  lambda: store.correct(EntityRef(kind="node", uid="A"),
                                        {"t": 3}, 0, 50)):
        write()
        per_step = store.check_trace(record)
        merged = store.check_scope(record["dependency"])
        assert not (merged.actionable_fresh and not per_step.actionable_fresh), (
            "the merged check went FRESH where the per-step fold did not — "
            "the widening is not monotone")


def test_the_merged_fallback_is_used_when_a_record_carries_no_steps(store):
    """An old record, or a result stored without its steps. Equally sound and
    strictly coarser — and it cannot attribute, so its witnesses carry no
    `step_id` and the map is keyed `"plan"` rather than an invented id."""
    record = _run(store)
    stripped = {k: v for k, v in record.items() if k != "steps"}
    store.assert_node("A", "N", {"tier": "gold"}, 0, 100)
    verdict = store.check_trace(stripped)
    assert not verdict.actionable_fresh
    assert [sid for sid, _v in verdict.per_step] == ["plan"]
    assert all(w.step_id is None for w in verdict.witnesses)


def test_a_record_with_neither_steps_nor_a_basis_refuses(store):
    """`TraceRecord.plan_basis` returns `{}` rather than inventing one when
    every step failed before a basis was recorded. There is nothing to check."""
    assert not store.check_trace({"plan_id": "x"}).actionable_fresh
    assert store.check_trace({"plan_id": "x"}).reasons == ("no-tt_q",)


def test_the_merged_scope_really_is_the_union_of_the_steps(store):
    record = _run(store)
    steps = [DependencyScope.from_json(s["dependency"]) for s in record["steps"]]
    assert DependencyScope.from_json(record["dependency"]).canonical() == \
        union_all(steps).canonical()


# ---------------------------------------------------------------------------
# (d) no new envelope key
# ---------------------------------------------------------------------------

def test_asking_the_question_writes_nothing_to_the_envelope(store):
    """A verdict is computed on demand and stored nowhere, so no comparator
    changes shape and no frozen digest can move."""
    router = ToolRouter(store.adapter, tt_source=store)
    env = router.call("entity_history", {"uid": "A"})
    before = json.dumps(env, sort_keys=True, default=str)
    store.check_result(env)
    store.check_scope(env["dependency"])
    assert json.dumps(env, sort_keys=True, default=str) == before
    assert "verdict" not in env and "freshness" not in env


def test_the_envelope_field_tuple_is_unchanged_by_m4():
    assert ENVELOPE_META_FIELDS == ("op", "args_echo", "dataset_extent",
                                    "tt_q", "pinned", "clamped", "dependency",
                                    "tgir")


def test_the_verdict_is_not_persisted_into_the_trace_record(store):
    record = _run(store)
    before = json.dumps(record, sort_keys=True, default=str)
    store.check_trace(record)
    assert json.dumps(record, sort_keys=True, default=str) == before


# ---------------------------------------------------------------------------
# the CLI verb — additive, and it renders the memo sentence
# ---------------------------------------------------------------------------

def _record_file(store) -> str:
    record = _run(store)
    path = Path(tempfile.mkdtemp()) / "record.json"
    path.write_text(json.dumps(record))
    return str(path)


def test_trace_check_renders_the_memo_sentence_from_a_real_witness(store, capsys):
    """(a) of the gate. The sentence is D13.27's, assembled from a witness this
    run produced — `tt` rendered as a wall-clock instant, `identity` naming the
    corrected thing, and `Reconsider.` as the last word, because the mechanism
    never repairs and never asserts a new answer."""
    path = _record_file(store)
    store.assert_node("A", "N", {"tier": "gold"}, 0, 100)
    code = main(["trace", "check", path, "--store", str(store.path)])
    out = capsys.readouterr().out
    assert code == 1
    assert "This answer may be stale." in out
    assert "s1:" in out
    assert "revised node A" in out
    assert "over a valid-time region this computation read" in out
    assert out.rstrip().endswith("Reconsider.")
    assert " UTC" in out, "tt renders as a wall-clock instant, not a bare int"


def test_trace_check_exits_zero_when_the_answer_still_holds(store, capsys):
    path = _record_file(store)
    assert main(["trace", "check", path, "--store", str(store.path)]) == 0
    assert "Nothing written since could have changed it" in capsys.readouterr().out


def test_trace_check_exits_nonzero_for_undecidable_too(store, capsys):
    """D13.25: `UNDECIDABLE` is not a third contract, so a script branching on
    the status code gets the conservative answer without having to know that."""
    record = _run(store)
    for step in record["steps"]:
        if "dependency" in step:
            step["dependency"]["store"] = "some-other-store"
    record["dependency"]["store"] = "some-other-store"
    path = Path(tempfile.mkdtemp()) / "r.json"
    path.write_text(json.dumps(record))
    assert main(["trace", "check", str(path), "--store", str(store.path)]) == 1
    assert "may be stale" in capsys.readouterr().out


def test_trace_check_json_emits_the_plan_verdict(store, capsys):
    path = _record_file(store)
    store.assert_node("A", "N", {"tier": "gold"}, 0, 100)
    main(["trace", "check", path, "--store", str(store.path), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"] == "possibly-stale"
    assert out["steps"]["s1"]["witnesses"][0]["matched_on"]
    assert out["steps"]["s2"]["verdict"] == "fresh"


def test_trace_check_as_of_asks_the_narrower_question(store, capsys):
    """`--as-of` is an "as of last Tuesday" question and the caller owns it.
    The default scans the whole suffix, because the log leads the frontier."""
    path = _record_file(store)
    cut = store.frontier_tt()
    store.assert_node("A", "N", {"tier": "gold"}, 0, 100)
    assert main(["trace", "check", path, "--store", str(store.path),
                 "--as-of", str(cut)]) == 0
    capsys.readouterr()
    assert main(["trace", "check", path, "--store", str(store.path)]) == 1


def test_trace_check_needs_a_store_and_says_why(store):
    path = _record_file(store)
    with pytest.raises(SystemExit):
        main(["trace", "check", path])


def _ask_record_file(store) -> str:
    """The `tgms ask --save-record` envelope — `{question, plan, trace, …}` —
    which is what `trace render` consumes."""
    path = Path(tempfile.mkdtemp()) / "ask.json"
    path.write_text(json.dumps({"question": "q?", "plan": PLAN,
                                "trace": _run(store), "answer": None}))
    return str(path)


def test_trace_render_still_works_verbatim(store, tmp_path):
    """The verb is **additive** (STABILITY.md): every existing
    `tgms trace render …` invocation keeps working unchanged."""
    out = tmp_path / "trace.html"
    assert main(["trace", "render", _ask_record_file(store), "-o", str(out)]) == 0
    assert out.read_text().lstrip().lower().startswith("<!doctype html")


def test_trace_check_accepts_the_same_ask_record_that_render_consumes(store, capsys):
    """One file, both actions. `ask --save-record` writes an envelope with the
    trace nested inside it; a bare `TraceRecord` is the inner object. A caller
    should not have to know which they hold."""
    path = _ask_record_file(store)
    assert main(["trace", "check", path, "--store", str(store.path)]) == 0
    capsys.readouterr()
    store.assert_node("A", "N", {"tier": "gold"}, 0, 100)
    assert main(["trace", "check", path, "--store", str(store.path)]) == 1
    assert "s1:" in capsys.readouterr().out


def test_trace_check_reads_the_log_and_never_opens_the_backend(store, tmp_path):
    """D13.20, made operational: the verb takes no database lock, needs no
    optional backend extra installed, and runs while a writer holds the store.
    Pointing it at a bare log file with no store beside it must work."""
    path = _record_file(store)
    store.assert_node("A", "N", {"tier": "gold"}, 0, 100)
    lonely = tmp_path / "eventlog.jsonl"
    lonely.write_bytes((store.path / "eventlog.jsonl").read_bytes())
    assert not (tmp_path / "store.duckdb").exists()
    assert main(["trace", "check", path, "--store", str(lonely)]) == 1


def test_trace_check_says_so_when_there_is_no_log(store, tmp_path):
    with pytest.raises(SystemExit):
        main(["trace", "check", _record_file(store), "--store", str(tmp_path)])


def test_trace_render_without_out_fails_where_it_used_to(store):
    """`-o` stopped being an argparse requirement so `check` need not carry it;
    the requirement moved into the dispatch, where it can name the action."""
    path = _record_file(store)
    with pytest.raises(SystemExit):
        main(["trace", "render", path])


def test_the_trace_parser_still_accepts_render_and_now_accepts_check():
    action = next(a for a in build_parser()._subparsers._group_actions[0]
                  .choices["trace"]._actions if a.dest == "action")
    assert action.choices == ["render", "check"]


# ---------------------------------------------------------------------------
# the chain cache is reachable from the surface and changes no verdict
# ---------------------------------------------------------------------------

def test_the_surface_forwards_a_chain_cache_without_changing_the_answer(store):
    record = _run(store)
    store.assert_node("A", "N", {"tier": "gold"}, 0, 100)
    cache = ChainCache()
    plain = store.check_trace(record).to_json()
    for _ in range(3):
        assert store.check_trace(record, chain_cache=cache).to_json() == plain
    assert cache.hits > 0


def test_check_scope_defaults_to_scanning_the_whole_suffix(store):
    """D-M4a. Passing this store's frontier as `tt_now` would be the
    false-fresh direction, because the log is fsynced before apply."""
    env = ToolRouter(store.adapter, tt_source=store).call(
        "entity_history", {"uid": "A"})
    store.assert_node("A", "N", {"tier": "gold"}, 0, 100)
    assert not store.check_scope(env["dependency"]).actionable_fresh
    assert store.check_scope(env["dependency"],
                             tt_now=env["tt_q"]).actionable_fresh
    assert isinstance(store.check_scope(env["dependency"], tt_now=OPEN_END),
                      Verdict)
