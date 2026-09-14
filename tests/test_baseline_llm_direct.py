"""[tests] LLM-direct baseline (Lane D5): the "just stuff the events in the
prompt" control -- no graph store, no retrieval index, no Cypher/SQL.

Fake-LLM driven, no network, no model: the answer object is scripted, so
what is tested is the arm's own mechanics -- deterministic event selection,
context-budget truncation, and the D-160 claim gate
(`tgms.eval.plan_faults.GATED_VERDICTS`) dropping a claim that cites
evidence outside the offered context while keeping a grounded one.
"""

from __future__ import annotations

import json

import pytest

import tgms
from tgms.core.model import canonical_json
from tgms.data.synth import generate
from tgms.eval.baselines import (
    LLMDirect, verify_llm_direct_claims,
)
from tgms.eval.tasks import generate_suite


def _contract_llm(answer_obj):
    return lambda model, messages, temperature, seed: canonical_json(answer_obj)


@pytest.fixture(scope="module")
def store_env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("llm_direct")
    generate(tmp / "synth", n_nodes=40, n_events=500, seed=11)
    store = tgms.open(tmp / "store")
    with open(tmp / "synth" / "events.jsonl") as f:
        store.ingest_events(json.loads(line) for line in f if line.strip())
    return {"tmp": tmp, "store": store}


# --------------------------------------------------------------------------- #
# (a) budget truncation is honoured and flagged                               #
# --------------------------------------------------------------------------- #

def test_budget_truncation_honoured_and_flagged(store_env):
    ao = {"text": "n/a", "claims": []}
    arm = LLMDirect(store_env["store"], _contract_llm(ao), "fake",
                    context_budget_tokens=20, seed=0)
    out = arm.answer("How many events are there?")
    meta = out["meta"]
    assert meta["events_offered"] > meta["events_included"]
    assert meta["truncated"] is True
    assert meta["events_included"] > 0  # at least one line always included
    assert meta["prompt_tokens_approx"] <= 20 or meta["events_included"] == 1

    # a budget wide enough for the whole corpus never truncates
    wide = LLMDirect(store_env["store"], _contract_llm(ao), "fake",
                     context_budget_tokens=1_000_000, seed=0)
    out_wide = wide.answer("How many events are there?")
    assert out_wide["meta"]["truncated"] is False
    assert out_wide["meta"]["events_included"] == out_wide["meta"]["events_offered"]


# --------------------------------------------------------------------------- #
# (b) event selection is deterministic across two runs                        #
# --------------------------------------------------------------------------- #

def test_event_selection_deterministic(store_env):
    seen = []

    def llm(model, messages, temperature, seed):
        seen.append(messages[-1]["content"])
        return canonical_json({"text": "n/a", "claims": []})

    arm1 = LLMDirect(store_env["store"], llm, "fake",
                     context_budget_tokens=500, seed=0)
    arm1.answer("Who talks to whom?")
    arm2 = LLMDirect(store_env["store"], llm, "fake",
                     context_budget_tokens=500, seed=0)
    arm2.answer("Who talks to whom?")
    assert seen[0] == seen[1]

    # entity-filtered selection is also stable across fresh instances
    e = store_env["store"].adapter.edges_columnar()
    seed_uid = store_env["store"].adapter.uids_for(e["src_id"][:1])[0]
    arm3 = LLMDirect(store_env["store"], llm, "fake",
                     context_budget_tokens=500, seed=0)
    arm3.answer("Who does this node talk to?", [seed_uid])
    arm4 = LLMDirect(store_env["store"], llm, "fake",
                     context_budget_tokens=500, seed=0)
    arm4.answer("Who does this node talk to?", [seed_uid])
    assert seen[2] == seen[3]
    assert seen[2] != seen[0]  # entity filter actually changed the context


# --------------------------------------------------------------------------- #
# verify_llm_direct_claims / gate unit behavior                                #
# --------------------------------------------------------------------------- #

def test_verify_marks_fabricated_evidence_unverifiable(store_env):
    arm = LLMDirect(store_env["store"], _contract_llm({}), "fake",
                    context_budget_tokens=2_000, seed=0)
    candidates = arm._select(None)
    lines, by_tag, _, _ = arm._serialize(candidates)
    assert by_tag  # sanity: at least one event offered
    real_tag = next(iter(by_tag))
    real_ev = by_tag[real_tag]
    answer_obj = {"text": "x", "claims": [
        {"id": "c1", "type": "entity", "uids": [real_ev["src"]],
         "evidence": [real_tag]},
        {"id": "c2", "type": "entity", "uids": ["totally-fake-uid"],
         "evidence": ["e999999"]},
        {"id": "c3", "type": "count", "value": 3, "evidence": [real_tag]},
    ]}
    report = verify_llm_direct_claims(answer_obj, by_tag)
    verdicts = {r["id"]: r["verdict"] for r in report["claims"]}
    assert verdicts["c1"] == "supported"
    assert verdicts["c2"] == "unverifiable"  # cites an event never offered
    assert verdicts["c3"] == "unverifiable"  # no recomputation for count/value


# --------------------------------------------------------------------------- #
# (c) 3-task CollegeMsg-shaped fixture through the harness's llm_direct arm    #
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def tiny_suite(store_env):
    manifest = generate(store_env["tmp"] / "synth2", n_nodes=25, n_events=150,
                        seed=5)
    store2 = tgms.open(store_env["tmp"] / "store2")
    with open(store_env["tmp"] / "synth2" / "events.jsonl") as f:
        store2.ingest_events(json.loads(line) for line in f if line.strip())
    raw = generate_suite(store2, "collegemsg-shaped", seed=2,
                         sizes={"t1": 6, "t3": 0, "t4": 0, "probes": 0},
                         manifest=manifest)
    tasks = sorted(raw["dev"] + raw["test"], key=lambda t: t["id"])[:3]
    assert len(tasks) == 3
    suite = {**raw, "dev": tasks, "test": []}
    store2.close()
    return {"tmp": store_env["tmp"], "suite": suite}


def test_llm_direct_registered_in_harness_arm_registry():
    from tgms.eval.harness import BASELINE_SYSTEMS
    assert "llm_direct" in BASELINE_SYSTEMS


def test_harness_llm_direct_end_to_end_gate_classifies_claims(tiny_suite,
                                                               tmp_path):
    from tgms.eval.harness import run_matrix

    suite = tiny_suite["suite"]
    suite_path = tiny_suite["tmp"] / "suite_llm_direct.json"
    suite_path.write_text(canonical_json(suite))

    task0_id = suite["dev"][0]["id"]
    # a fake answer with one claim grounded in whatever the arm actually
    # offers (tag "e0", always present when any event was selected) and one
    # citing an evidence id that can never exist in the offered context --
    # the gate must classify the latter `unverifiable` and drop it.
    answer_obj = {"text": "two claims, one fabricated.", "claims": [
        {"id": "c1", "type": "entity", "uids": ["will-not-match-anything"],
         "evidence": ["e0"]},
        {"id": "c2", "type": "count", "value": 999,
         "evidence": ["e_nonexistent_9999"]}]}
    llm = _contract_llm(answer_obj)

    cfg = {
        "suite_path": str(suite_path),
        "store_path": str(tiny_suite["tmp"] / "store2"),
        "out_dir": str(tmp_path / "out"),
        "systems": ["llm_direct"],
        "models": ["fake"],
        "seeds": [0],
        "split": "dev",
    }
    rows = run_matrix(cfg, llm_fn=llm)
    assert len(rows) == 3
    row0 = next(r for r in rows if r["task_id"] == task0_id)
    report = row0["meta"]["report"]
    verdicts = [c["verdict"] for c in report["claims"]]
    assert "unverifiable" in verdicts
    # the fabricated-evidence claim must not survive into the delivered
    # answer object (same drop set the `ours` production gate uses)
    kept_ids = [c["id"] for c in row0["answer_object"]["claims"]]
    assert "c2" not in kept_ids
    assert row0["meta"]["n_claims_dropped"] >= 1

    # cache makes a rerun free and identical
    rows2 = run_matrix(cfg, llm_fn=llm)
    assert len(rows2) == len(rows)
