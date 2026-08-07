"""[tests] Oracle-v3 inventory contract (D-098): every draw is a record.

New tests for new machinery, no ground-truth changes. What is pinned: the
fixed draw universe (no refill — the refill loop was D-091's
composition-shifting mechanism), record completeness for every outcome,
the two-lane gold receipt, production-admission labeling under the frozen
policy, and the suites being exactly the resolved+eligible view.
"""

from __future__ import annotations

import json

import pytest

import tgms
from tgms.data.synth import generate
from tgms.eval.tasks import generate_suite

REQUIRED_KEYS = {
    "task_id", "dataset", "source", "family", "question_text",
    "requested_claim_type", "answerability", "oracle_status", "oracle_gold",
    "gold_answer_object", "oracle_receipt", "production_admission",
    "expressibility", "suite_eligible", "ineligible_reason",
}

SIZES = {"t1": 20, "t3": 9, "t4": 8, "probes": 4}


@pytest.fixture(scope="module")
def v3_env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("v3")
    generate(tmp / "synth", n_nodes=200, n_events=4000, seed=3,
             n_rings=2, n_pingpong=1, n_bursts=1)
    store = tgms.open(tmp / "store")
    with open(tmp / "synth" / "events.jsonl") as f:
        store.ingest_events(json.loads(line) for line in f if line.strip())
    suite = generate_suite(store, "synth-v3", seed=1, sizes=SIZES)
    return {"store": store, "suite": suite}


def test_fixed_draw_universe(v3_env):
    recs = [r for r in v3_env["suite"]["records"] if r["source"] == "template"
            and r["family"] in ("t1", "t3", "t4")]
    # every draw is a record: the universe is the draw count, not the kept
    # count — a failed draw must not be replaced by a fresh one
    assert len(recs) == SIZES["t1"] + SIZES["t3"] + SIZES["t4"]


def test_every_record_is_complete(v3_env):
    for r in v3_env["suite"]["records"]:
        missing = REQUIRED_KEYS - set(r)
        assert not missing, f"{r['task_id']}: missing {missing}"


def test_not_applicable_draws_are_records(v3_env):
    # synth is single-rel-type, so every rel_type_count draw must appear as
    # a not-applicable record rather than disappearing
    na = [r for r in v3_env["suite"]["records"]
          if r.get("ineligible_reason") == "template_not_applicable"]
    assert na, "expected not-applicable records on a single-type store"
    for r in na:
        assert r["question_text"] is None
        assert r["oracle_status"] == "oracle_unsupported"
        assert r["suite_eligible"] is False


def test_suites_are_the_resolved_eligible_view(v3_env):
    suite = v3_env["suite"]
    by_id = {r["task_id"]: r for r in suite["records"]}
    for t in suite["dev"] + suite["test"]:
        r = by_id[t["id"]]
        assert r["oracle_status"] == "resolved"
        assert r["suite_eligible"] is True
        assert t["gold"] == r["oracle_gold"]


def test_admission_labels_carry_the_policy(v3_env):
    labelled = [r for r in v3_env["suite"]["records"]
                if r.get("production_admission")]
    assert labelled
    for r in labelled:
        pa = r["production_admission"]
        assert pa["outcome"] in ("admitted", "refused", "failed", "timeout")
        assert pa["policy_version"] == "guardrail-policy-v1"


def test_regeneration_is_idempotent(v3_env):
    again = generate_suite(v3_env["store"], "synth-v3", seed=1, sizes=SIZES)
    assert again["test_split_sha"] == v3_env["suite"]["test_split_sha"]
    assert [r["task_id"] for r in again["records"]] == \
        [r["task_id"] for r in v3_env["suite"]["records"]]
