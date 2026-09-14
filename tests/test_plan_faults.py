"""Lane E, task E1: the plan-level fault matrix and four-way classifier.

NORMATIVE: `docs/design/TRUST_BOUNDARY_FAULT_MATRIX_DESIGN_2026-09-13.md`
(FROZEN). No store, no LLM for the fixture/unit tests below (§6); the
smoke test at the bottom is the one exception, and it is skipped outright
when the native engine extension or the frozen event log is unavailable.
"""

from __future__ import annotations

import json
import random
import subprocess
import sys
from pathlib import Path

import pytest

from tgms.eval.plan_faults import (
    CELL_REGISTRY, EXEC_FAULTS, F1_CELL_IDS, F2_CELL_IDS, Oracle, Outcome,
    PLAN_MUTATORS, Run, TGIR_MUTATORS, classify, gate_answer,
)

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "plan_faults" /
                      "ten_cases.json").read_text())["cases"]

_NA_KEYS = ("gold", "recompute_value")


def _build(case: dict) -> tuple[Run, Oracle]:
    r = case["run"]
    run = Run(certificate=r["certificate"], freshness=r["freshness"],
              answer=r["answer"], answer_value=r["answer_value"],
              report=r["report"], error=r["error"])
    oracle_kwargs = {k: v for k, v in case["oracle"].items() if k in _NA_KEYS}
    oracle = Oracle(**oracle_kwargs)
    return run, oracle


# --------------------------------------------------------------------------- #
# the 10-case fixture: every outcome class, every invariant, orthogonality    #
# --------------------------------------------------------------------------- #

def test_fixture_has_exactly_ten_cases_covering_every_outcome():
    assert len(FIXTURE) == 10
    by_outcome: dict[str, int] = {}
    for case in FIXTURE:
        by_outcome[case["expected_outcome"]] = by_outcome.get(case["expected_outcome"], 0) + 1
    assert by_outcome == {"correct": 2, "safe-refusal": 3,
                          "explicit-failure": 2, "silent-violation": 3}
    assert set(o.value for o in Outcome) == set(by_outcome)


@pytest.mark.parametrize("case", FIXTURE, ids=[c["id"] for c in FIXTURE])
def test_classify_matches_fixture_expectation(case):
    run, oracle = _build(case)
    cls = classify(run, oracle)
    assert cls.outcome.value == case["expected_outcome"], case["id"]
    assert cls.gold_mismatch == case["expected_gold_mismatch"], case["id"]
    assert cls.misattributed == case["expected_misattributed"], case["id"]
    got_invariants = sorted({i for i, _c, _d in cls.invariants})
    assert got_invariants == sorted(case["expected_invariants"]), case["id"]
    if "expected_reason" in case:
        assert cls.reason == case["expected_reason"], case["id"]


def test_classify_is_total_and_pure_over_the_fixture():
    """Every case reaches exactly one `return` in `classify` (no exception,
    no missing outcome), and calling it twice on the same inputs gives the
    same `Classification` -- purity."""
    for case in FIXTURE:
        run, oracle = _build(case)
        first = classify(run, oracle)
        second = classify(run, oracle)
        assert first.to_json() == second.to_json()
        assert isinstance(first.outcome, Outcome)


def test_correct_and_gold_mismatch_are_orthogonal():
    """Fixture case 2: a claim fully supported by its own trace (`correct`
    under I1-I4) that still disagrees with gold. The two flags must not be
    coupled in either direction -- §4: 'orthogonal, always recorded, never
    part of the headline.'"""
    case = next(c for c in FIXTURE if c["id"] == "correct-but-gold-mismatch")
    run, oracle = _build(case)
    cls = classify(run, oracle)
    assert cls.outcome is Outcome.CORRECT
    assert cls.gold_mismatch is True
    # and the plain-correct case has no gold at all -> None, not False
    plain = next(c for c in FIXTURE if c["id"] == "correct-plain")
    run2, oracle2 = _build(plain)
    cls2 = classify(run2, oracle2)
    assert cls2.outcome is Outcome.CORRECT
    assert cls2.gold_mismatch is None


def test_gold_mismatch_is_not_applicable_with_no_answer():
    """Named deviation (plan_faults.py `_finalize`): a failed/refused run
    has no answer_value to compare, so gold_mismatch stays `None`, not a
    vacuous `True` from `None != gold`."""
    run = Run(error={"error": "E_SCHEMA", "message": "x", "details": {}})
    cls = classify(run, Oracle(gold=7))
    assert cls.outcome is Outcome.EXPLICIT_FAILURE
    assert cls.gold_mismatch is None


# --------------------------------------------------------------------------- #
# a test per outcome class, independent of the fixture file                  #
# --------------------------------------------------------------------------- #

def test_outcome_correct():
    run = Run(answer={"claims": []}, answer_value=1, report={"claims": []})
    assert classify(run).outcome is Outcome.CORRECT


def test_outcome_safe_refusal_certificate():
    run = Run(certificate={"reason": "no-admissible-plan"})
    cls = classify(run)
    assert cls.outcome is Outcome.SAFE_REFUSAL
    assert cls.reason == "no-admissible-plan"


def test_outcome_safe_refusal_undecidable_freshness():
    run = Run(freshness={"kind": "UNDECIDABLE"})
    cls = classify(run)
    assert cls.outcome is Outcome.SAFE_REFUSAL
    assert cls.reason == "undecidable-freshness"


def test_outcome_explicit_failure():
    run = Run(error={"error": "E_SCHEMA", "message": "bad plan", "details": {}})
    cls = classify(run)
    assert cls.outcome is Outcome.EXPLICIT_FAILURE
    assert cls.reason == "E_SCHEMA"


def test_outcome_silent_violation():
    run = Run(
        answer={"claims": [{"id": "c1", "type": "count", "value": 5,
                           "evidence": ["s1"]}]},
        answer_value=5,
        report={"claims": [{"id": "c1", "verdict": "unsupported",
                           "reason": "x", "truncated": False,
                           "cited_uids": [], "cited_values": [3]}]})
    cls = classify(run)
    assert cls.outcome is Outcome.SILENT_VIOLATION
    assert {i for i, _c, _d in cls.invariants} == {"I1", "I3"}


def test_classify_is_total_asserts_on_a_malformed_run():
    """`run.answer is None` with no error and no certificate is not a valid
    `Run` -- §4's own assertion, kept verbatim rather than swallowed."""
    with pytest.raises(AssertionError):
        classify(Run())


# --------------------------------------------------------------------------- #
# gate_answer: unsupported AND unverifiable are dropped before delivery      #
# --------------------------------------------------------------------------- #

def test_gate_answer_drops_unsupported_and_unverifiable():
    answer = {"text": "t", "claims": [
        {"id": "c1", "type": "count", "value": 1, "evidence": ["s1"]},
        {"id": "c2", "type": "count", "value": 2, "evidence": ["s2"]},
        {"id": "c3", "type": "count", "value": 3, "evidence": ["s3"]},
    ]}
    report = {"claims": [
        {"id": "c1", "verdict": "supported"},
        {"id": "c2", "verdict": "unsupported"},
        {"id": "c3", "verdict": "unverifiable"},
    ]}
    gated, gated_report, n_dropped = gate_answer(answer, report)
    assert [c["id"] for c in gated["claims"]] == ["c1"]
    assert [r["id"] for r in gated_report["claims"]] == ["c1"]
    assert n_dropped == 2


# --------------------------------------------------------------------------- #
# determinism: same seed -> identical trials digest                          #
# --------------------------------------------------------------------------- #

_SAMPLE_PLAN = {
    "plan_id": "det-1",
    "question": "sample",
    "steps": [
        {"id": "s1", "op": "entity_history",
         "args": {"uid": "n1", "as_of_tt": 100, "limit": 10},
         "depends_on": []},
        {"id": "s2", "op": "compute",
         "args": {"fn": "count", "input": {"$ref": "s1.rows"}},
         "depends_on": ["s1"]},
    ],
    "answer_spec": {"kind": "count", "from": "s2.value"},
}


def _mutate_many(cell_id: str, seed: int, n: int = 25) -> list[dict]:
    mutator = PLAN_MUTATORS[cell_id]
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        m = mutator(_SAMPLE_PLAN, rng, {})
        out.append(m)
    return out


@pytest.mark.parametrize("cell_id", ["F1-1", "F1-2", "F1-4", "F1-5"])
def test_mutator_determinism_same_seed_same_digest(cell_id):
    from tgms.core.model import canonical_json, sha256_hex

    a = _mutate_many(cell_id, seed=7)
    b = _mutate_many(cell_id, seed=7)
    assert sha256_hex(canonical_json(a)) == sha256_hex(canonical_json(b))
    c = _mutate_many(cell_id, seed=8)
    # not asserting inequality unconditionally (a mutator may legitimately
    # be seed-insensitive on a single-shape sample plan), but the two
    # matching seeds must never disagree, which is the actual property
    assert isinstance(c, list)


def test_run_plan_cell_determinism(monkeypatch):
    """The same property one level up: `scripts/eval_fault_matrix.py`'s own
    trial loop, seeded, over a fixed in-memory plan pool with no store
    needed for the *static* stage (validate_static accepts adapter=None)."""
    from tgms.agent.verifier import validate_static
    from tgms.core.model import canonical_json, sha256_hex

    def trials_for(seed: int) -> list[dict]:
        rng = random.Random(seed)
        out = []
        for _ in range(10):
            mutant = PLAN_MUTATORS["F1-4"](_SAMPLE_PLAN, rng, {})
            if mutant is None:
                continue
            result = validate_static(mutant, adapter=None, task_input_uids={"n1"})
            out.append({"valid": result["valid"],
                       "violations": result["violations"]})
        return out

    d1 = sha256_hex(canonical_json(trials_for(3)))
    d2 = sha256_hex(canonical_json(trials_for(3)))
    assert d1 == d2


# --------------------------------------------------------------------------- #
# every mutator either returns a differing plan or None (§6's convention)    #
# --------------------------------------------------------------------------- #

_UID_PLAN = {
    "plan_id": "uid-1", "question": "q",
    "steps": [
        {"id": "s1", "op": "entity_history",
         "args": {"uid": "n1", "as_of_tt": 100,
                  "window": {"t_a": 0, "t_b": 1000}, "limit": 10},
         "depends_on": []},
    ],
    "answer_spec": {"kind": "value", "from": "s1.rows"},
}


@pytest.mark.parametrize("cell_id", sorted(PLAN_MUTATORS))
def test_plan_mutator_returns_none_or_a_different_plan(cell_id):
    rng = random.Random(0)
    for plan in (_SAMPLE_PLAN, _UID_PLAN):
        out = PLAN_MUTATORS[cell_id](plan, rng, {})
        assert out is None or out != plan


_TGIR_ROOT = {
    "op": "NodeScan", "as": "n", "uids": ["1", "2"],
}


@pytest.mark.parametrize("cell_id", sorted(TGIR_MUTATORS))
def test_tgir_mutator_returns_none_or_a_different_root(cell_id):
    rng = random.Random(0)
    out = TGIR_MUTATORS[cell_id](_TGIR_ROOT, rng,
                                 {"sigma": {"t_v": [[0, 1000]], "t_b": 1000}})
    assert out is None or out != _TGIR_ROOT


# --------------------------------------------------------------------------- #
# the cell registry mirrors the frozen memo's tables exactly (drift guard)   #
# --------------------------------------------------------------------------- #

#: Transcribed directly from §2 (F1) and §3 (F2) of
#: docs/design/TRUST_BOUNDARY_FAULT_MATRIX_DESIGN_2026-09-13.md. A change to
#: either table that is not mirrored here fails this test -- that is the
#: point (§6: "encode the table as data ... so drift from the frozen memo
#: is caught").
FROZEN_TABLE: dict[str, dict] = {
    "F1-1": {"fault": "fabricated entity id",
            "expected_outcome_text": "explicit-failure", "invariant": "I2"},
    "F1-2": {"fault": "nonexistent property",
            "expected_outcome_text": "explicit-failure", "invariant": "I1/I3"},
    "F1-3a": {"fault": "invalid interval",
             "expected_outcome_text": "explicit-failure", "invariant": "I3"},
    "F1-3b": {"fault": "valid but wrong interval",
             "expected_outcome_text": "correct + gold-mismatch", "invariant": "-"},
    "F1-4": {"fault": "invalid primitive",
            "expected_outcome_text": "explicit-failure", "invariant": "I1"},
    "F1-5": {"fault": "malformed plan",
            "expected_outcome_text": "explicit-failure", "invariant": "I1"},
    "F1-6": {"fault": "semantically wrong operator",
            "expected_outcome_text": "correct + gold-mismatch", "invariant": "-"},
    "F1-7": {"fault": "arithmetic outside trusted operator",
            "expected_outcome_text": "explicit-failure (gated)", "invariant": "I3"},
    "F1-8": {"fault": "unsupported aggregation",
            "expected_outcome_text": "safe-refusal", "invariant": "I1/I3"},
    "F1-9": {"fault": "stale trace reference",
            "expected_outcome_text": "explicit-failure", "invariant": "I1"},
    "F1-10": {"fault": "unsupported final claim",
             "expected_outcome_text": "explicit-failure (gated)", "invariant": "I1/I2"},
    "F1-11": {"fault": "claim over truncated output",
             "expected_outcome_text": "safe-refusal (weak support)", "invariant": "I1"},
    "F2-1": {"fault": "operator exception",
            "expected_outcome_text": "explicit-failure", "invariant": "I1"},
    "F2-2": {"fault": "timeout",
            "expected_outcome_text": "explicit-failure", "invariant": "I1"},
    "F2-3": {"fault": "partial result (silent)",
            "expected_outcome_text": "silent-violation by construction",
            "invariant": "-"},
    "F2-4": {"fault": "honest truncation",
            "expected_outcome_text": "safe-refusal", "invariant": "I1"},
    "F2-5": {"fault": "resource-limit violation",
            "expected_outcome_text": "safe-refusal", "invariant": "-"},
    "F2-6": {"fault": "unavailable index",
            "expected_outcome_text": "explicit-failure, or correct if a "
                                     "scan fallback reproduces O-ENV",
            "invariant": "I1"},
    "F2-7": {"fault": "stale index metadata",
            "expected_outcome_text": "by reference", "invariant": "I4"},
}


def test_every_frozen_cell_is_registered_exactly_once():
    assert set(CELL_REGISTRY) == set(FROZEN_TABLE)
    assert len(CELL_REGISTRY) == len(FROZEN_TABLE) == 19
    for cell_id, expected in FROZEN_TABLE.items():
        cell = CELL_REGISTRY[cell_id]
        assert cell.fault == expected["fault"], cell_id
        assert cell.expected_outcome_text == expected["expected_outcome_text"], cell_id
        assert cell.invariant == expected["invariant"], cell_id


def test_f1_and_f2_ids_partition_the_registry():
    assert set(F1_CELL_IDS) | set(F2_CELL_IDS) == set(CELL_REGISTRY)
    assert set(F1_CELL_IDS) & set(F2_CELL_IDS) == set()
    assert len(F1_CELL_IDS) == 12  # 1,2,3a,3b,4,5,6,7,8,9,10,11
    assert len(F2_CELL_IDS) == 7


def test_headline_exclusions_match_section_8():
    """§8: F1-3b, F1-6, F2-3 predict zero detections/zero I-violations and
    are reported outside the headline; F2-7 is by-reference, never run."""
    non_headline = {c for c, cell in CELL_REGISTRY.items() if not cell.headline}
    assert non_headline == {"F1-3b", "F1-6", "F2-3", "F2-7"}


def test_safe_refusal_reason_taxonomy_is_closed_and_covers_registered_cells():
    from tgms.eval.plan_faults import SAFE_REFUSAL_REASONS, to_certificate

    # a code/message combination *inside* the closed taxonomy
    assert to_certificate({"error": "E_COST", "message": "x", "details": {}}
                         )["reason"] in SAFE_REFUSAL_REASONS
    assert to_certificate({"error": "E_NO_PLAN", "message": "x", "details": {}}
                         )["reason"] in SAFE_REFUSAL_REASONS
    # anything else is outside it -> None -> explicit-failure downstream
    assert to_certificate({"error": "E_SCHEMA", "message": "x", "details": {}}) is None
    assert to_certificate({"error": "E_INVALID_ARG", "message": "x", "details": {}}) is None
    assert to_certificate(None) is None


def test_every_plan_mutators_key_is_a_registered_or_documented_cell():
    """PLAN_MUTATORS/TGIR_MUTATORS keys are either a frozen cell id, or
    'F1-6b' -- the oracled sub-case of F1-6 the memo directs be run "as a
    separate cell" (§2) without adding a 12th row to the frozen F1 table."""
    for key in PLAN_MUTATORS:
        assert key in CELL_REGISTRY or key == "F1-6b", key
    for key in TGIR_MUTATORS:
        assert key in CELL_REGISTRY, key
    for key in EXEC_FAULTS:
        assert key in CELL_REGISTRY, key


# --------------------------------------------------------------------------- #
# smoke: the driver script, --n 2, two cells, on stores/collegemsg           #
# --------------------------------------------------------------------------- #

ROOT = Path(__file__).resolve().parents[1]
FROZEN_LOG = ROOT / "benchmarks" / "frozen-v1" / "collegemsg.eventlog.jsonl"


@pytest.mark.skipif(not FROZEN_LOG.exists(), reason="no frozen collegemsg event log")
def test_smoke_run_writes_a_manifest_with_the_required_fields(tmp_path):
    pytest.importorskip("tgms._engine", reason="native engine extension not built")
    store_dir = tmp_path / "stores" / "collegemsg"
    out_dir = tmp_path / "faults-out"
    script = ROOT / "scripts" / "eval_fault_matrix.py"
    env = {"PYTHONPATH": str(ROOT), "TGMS_TEST_BACKEND": "native"}
    import os
    full_env = {**os.environ, **env}
    result = subprocess.run(
        [sys.executable, str(script), "--suite", "collegemsg",
        "--cells", "F1-1,F1-4", "--n", "2", "--seed", "0",
        "--store", str(store_dir), "--out", str(out_dir)],
        capture_output=True, text=True, env=full_env, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr

    for cell in ("F1-1", "F1-4"):
        manifest_path = out_dir / f"{cell}-collegemsg.json"
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text())
        for key in ("git_commit", "timestamp_utc", "machine", "seed",
                   "dataset", "result_digest", "protocol", "per_case"):
            assert key in manifest, (cell, key)
        assert manifest["machine"].keys() >= {"host", "platform", "cpus"}
        assert manifest["dataset"]["name"] == "collegemsg"
        assert manifest["dataset"]["digest"]
        assert manifest["seed"] == 0
        assert len(manifest["per_case"]) == manifest["n_cases"]
        for case in manifest["per_case"]:
            assert case["outcome"] in {o.value for o in Outcome}


def test_dry_run_lists_cells_without_a_store():
    """`--dry-run` never opens a store or touches the network -- runs even
    without the frozen event log or the native engine."""
    script = ROOT / "scripts" / "eval_fault_matrix.py"
    import os
    result = subprocess.run(
        [sys.executable, str(script), "--dry-run"],
        capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT)}, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 19  # all frozen cells, "all" excludes only F2-7
    for cell_id in CELL_REGISTRY:
        if cell_id == "F2-7":
            continue
        assert any(line.startswith(cell_id + "\t") for line in lines), cell_id
