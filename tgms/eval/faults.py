"""Fault-injection harness for verifier validation (WP2.3 / C2).

Perturbs correct AnswerObjects — ±1 counts, swapped uids, shifted intervals,
inverted orderings — and measures (a) mutant detection rate (mutated claim
verdict becomes unsupported) and (b) false-positive rate on unperturbed
answers. Acceptance: >= 95% detection, < 5% FP.
"""

from __future__ import annotations

import copy
import random
from typing import Any, Callable

from tgms.agent.verifier import ClaimVerifier

Mutator = Callable[[dict[str, Any], random.Random], dict[str, Any] | None]


def _mutable_claims(ans: dict[str, Any], types: set[str]) -> list[int]:
    return [i for i, c in enumerate(ans["claims"]) if c["type"] in types]


def mutate_count(ans: dict[str, Any], rng: random.Random) -> dict[str, Any] | None:
    idx = _mutable_claims(ans, {"count", "value"})
    idx = [i for i in idx if isinstance(ans["claims"][i].get("value"), (int, float))]
    if not idx:
        return None
    out = copy.deepcopy(ans)
    c = out["claims"][rng.choice(idx)]
    bump = rng.choice([-1, 1])
    c["value"] = c["value"] + bump
    # keep the prose consistent with the (now false) claim
    out["text"] = out["text"].replace(str(c["value"] - bump), str(c["value"]), 1)
    return out


def swap_uid(ans: dict[str, Any], rng: random.Random) -> dict[str, Any] | None:
    idx = [i for i in _mutable_claims(ans, {"entity"})
           if ans["claims"][i].get("uids")]
    if not idx:
        return None
    out = copy.deepcopy(ans)
    c = out["claims"][rng.choice(idx)]
    j = rng.randrange(len(c["uids"]))
    c["uids"][j] = f"fabricated-{rng.randrange(10_000)}"
    return out


def shift_interval(ans: dict[str, Any], rng: random.Random) -> dict[str, Any] | None:
    idx = [i for i in _mutable_claims(ans, {"temporal_pattern", "ordering"})]
    if not idx:
        return None
    out = copy.deepcopy(ans)
    c = out["claims"][rng.choice(idx)]
    key = "interval" if "interval" in c else "a"
    iv = c.get(key)
    if not isinstance(iv, dict):
        return None
    span_keys = [k for k in ("t_a", "t_b", "start", "end", "t") if k in iv]
    if not span_keys:
        return None
    lo, hi = min(iv[k] for k in span_keys), max(iv[k] for k in span_keys)
    shift = max(1, (hi - lo)) * rng.choice([-3, 3])
    for k in span_keys:
        iv[k] += shift
    return out


def invert_ordering(ans: dict[str, Any], rng: random.Random) -> dict[str, Any] | None:
    idx = [i for i in _mutable_claims(ans, {"ordering"})
           if isinstance(ans["claims"][i].get("relation"), str)]
    if not idx:
        return None
    out = copy.deepcopy(ans)
    c = out["claims"][rng.choice(idx)]
    inverse = {"before": "after", "after": "before", "meets": "met_by",
               "met_by": "meets", "during": "contains", "contains": "during",
               "overlaps": "overlapped_by", "overlapped_by": "overlaps",
               "starts": "started_by", "started_by": "starts",
               "finishes": "finished_by", "finished_by": "finishes",
               "equals": "before"}
    c["relation"] = inverse.get(c["relation"], "before")
    return out


MUTATORS: dict[str, Mutator] = {
    "count_pm1": mutate_count,
    "uid_swap": swap_uid,
    "interval_shift": shift_interval,
    "ordering_invert": invert_ordering,
}


def c2_readout_from_suite(store: Any, suite: dict[str, Any],
                          n_mutants: int = 500, seed: int = 0,
                          scratch_dir: str | None = None) -> dict[str, Any]:
    """The C2 acceptance experiment (spec WP2.3): execute every oracle plan
    in the suite, derive the correct AnswerObject mechanically (always
    grounded), then measure mutant detection and FP rates. No LLM involved."""
    import tempfile
    from pathlib import Path

    from tgms.agent.executor import Executor, ResultStore
    from tgms.agent.ir import Plan
    from tgms.agent.reporter import mechanical_answer
    from tgms.tools.server import ToolRouter

    scratch = Path(scratch_dir or tempfile.mkdtemp(prefix="tgms-c2-"))
    results = ResultStore(scratch / "results")
    executor = Executor(ToolRouter(store.adapter), results)
    answers: list[tuple[dict[str, Any], ClaimVerifier]] = []
    for task in suite["dev"] + suite["test"]:
        if task.get("gold_source") != "oracle_plan":
            continue
        plan = Plan.from_json(task["oracle_plan"])
        trace = executor.run(plan)
        if not trace.ok:
            continue
        ans = mechanical_answer(plan, trace)
        if not ans["claims"]:
            continue
        verifier = ClaimVerifier(trace, results, store.adapter)
        report = verifier.verify(ans)
        if all(c["verdict"] in ("supported", "weakly_supported")
               for c in report["claims"]):
            answers.append((ans, verifier))
    stats = run_fault_injection(answers, n_mutants=n_mutants, seed=seed)
    stats["accepted"] = (stats["detection_rate"] >= 0.95
                         and stats["fp_rate"] < 0.05)
    return stats


def run_fault_injection(answers: list[tuple[dict[str, Any], ClaimVerifier]],
                        n_mutants: int = 500, seed: int = 0) -> dict[str, Any]:
    """answers: (correct AnswerObject, its verifier). Returns detection/FP
    stats overall and per mutator."""
    rng = random.Random(seed)

    # A claim is "caught" when it no longer verifies as supported: fabricated
    # values grounded nowhere come back `unverifiable`, contradicted values
    # come back `unsupported` — both mean the answer fails verification.
    def rejected(report: dict[str, Any]) -> bool:
        return any(c["verdict"] in ("unsupported", "unverifiable")
                   for c in report["claims"])

    # false positives: unperturbed answers must verify clean
    fp = 0
    for ans, verifier in answers:
        fp += rejected(verifier.verify(ans))

    per: dict[str, dict[str, int]] = {m: {"made": 0, "detected": 0}
                                      for m in MUTATORS}
    made = 0
    while made < n_mutants:
        progressed = False
        for name, fn in MUTATORS.items():
            if made >= n_mutants:
                break
            ans, verifier = answers[rng.randrange(len(answers))]
            mutant = fn(ans, rng)
            if mutant is None:
                continue
            progressed = True
            made += 1
            per[name]["made"] += 1
            if rejected(verifier.verify(mutant)):
                per[name]["detected"] += 1
        if not progressed:
            break  # no mutator applies to this answer pool
    total_detected = sum(v["detected"] for v in per.values())
    return {
        "n_answers": len(answers),
        "false_positives": fp,
        "fp_rate": fp / len(answers) if answers else 0.0,
        "n_mutants": made,
        "detected": total_detected,
        "detection_rate": total_detected / made if made else 0.0,
        "per_mutator": per,
    }
