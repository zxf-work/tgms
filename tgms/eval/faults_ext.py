"""Extended fault-injection classes (CIDR campaign): mutations that exercise
database-specific evidence semantics — completeness, belief state, set
membership, citation, and units — beyond the count/uid classes of
`tgms.eval.faults`.

Two protocols coexist here:

* answer mutators, `(answer, rng, ctx) -> mutant | None`, applied to
  mechanically derived correct answers exactly as in faults.py, with a
  shared `ctx` giving access to the trace, other step ids, and a store-wide
  uid pool;
* case generators (`truncated_count_cases`, `wrong_belief_state_cases`)
  that re-execute oracle plans under modified conditions (a small page
  limit; a stripped belief state) to produce claims that are arithmetically
  correct yet false as statements about the database.

Detection means the mutated claim no longer verifies as fully supported:
`unsupported` and `unverifiable` count (D-006), and for completeness
classes `weakly_supported` counts as detection too — the whole point of
truncation taint is refusing full support.

Known negative, reported rather than hidden: dropping a member from an
entity set is undetectable at the claim level, because entity checking is
grounding (claimed uids must appear in the cited evidence), not set
equality against a gold answer. Under-claiming passes; scoring against
gold catches it, the trace verifier does not.
"""

from __future__ import annotations

import copy
import random
from typing import Any, Callable

from tgms.agent.verifier import ClaimVerifier, _collect_all_strings

ExtMutator = Callable[[dict[str, Any], random.Random, dict[str, Any]],
                      dict[str, Any] | None]

EPOCH_MICROS_MIN = 10 ** 14  # numbers above this are treated as timestamps


# ------------------------------------------------------------------ #
# claim factories: enrich mechanical answers with correct ordering / #
# pattern claims so those mutation classes have volume               #
# ------------------------------------------------------------------ #

def _collect_numbers(obj: Any, out: set[float]) -> None:
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        out.add(float(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect_numbers(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _collect_numbers(v, out)


def factory_ordering(trace: Any, results: Any) -> list[dict[str, Any]]:
    """Correct ordering claims from pairs of timestamps found in ok steps."""
    from tgms.temporal.ops_compute import allen_relation

    claims = []
    for rec in trace.steps:
        if rec.get("status") != "ok" or results is None:
            continue
        nums: set[float] = set()
        _collect_numbers(results.get(rec["result_digest"]), nums)
        ts = sorted(n for n in nums if n >= EPOCH_MICROS_MIN)
        if len(ts) < 2:
            continue
        t1, t2 = int(ts[0]), int(ts[-1])
        if t1 == t2:
            continue
        a, b = {"t": t1}, {"t": t2}
        rel = allen_relation({"start": t1, "end": t1 + 1},
                             {"start": t2, "end": t2 + 1})
        claims.append({"id": f"fo-{rec['step_id']}", "type": "ordering",
                       "a": a, "b": b, "relation": rel,
                       "evidence": [rec["step_id"]]})
    return claims


def factory_pattern(trace: Any, results: Any) -> list[dict[str, Any]]:
    """Correct burst claims from flagged buckets of burst_detection steps."""
    claims = []
    for rec in trace.steps:
        if rec.get("status") != "ok" or rec.get("op") != "burst_detection" \
                or results is None:
            continue
        payload = results.get(rec["result_digest"])
        for row in (payload.get("rows") or [])[:2]:
            if "t_a" in row and "t_b" in row:
                claims.append({"id": f"fp-{rec['step_id']}-{row['t_a']}",
                               "type": "temporal_pattern",
                               "assertion": "burst",
                               "interval": {"t_a": row["t_a"],
                                            "t_b": row["t_b"]},
                               "evidence": [rec["step_id"]]})
    return claims


# ------------------------------------------------------------------ #
# extended answer mutators                                           #
# ------------------------------------------------------------------ #

def _claims_of(ans: dict[str, Any], types: set[str]) -> list[int]:
    return [i for i, c in enumerate(ans["claims"]) if c["type"] in types]


def entity_drop(ans: dict[str, Any], rng: random.Random,
                ctx: dict[str, Any]) -> dict[str, Any] | None:
    idx = [i for i in _claims_of(ans, {"entity"})
           if len(ans["claims"][i].get("uids") or []) >= 2]
    if not idx:
        return None
    out = copy.deepcopy(ans)
    c = out["claims"][rng.choice(idx)]
    c["uids"].pop(rng.randrange(len(c["uids"])))
    return out


def entity_add(ans: dict[str, Any], rng: random.Random,
               ctx: dict[str, Any]) -> dict[str, Any] | None:
    """Add a REAL store entity that the cited evidence does not contain —
    plausible over-claiming, unlike uid_swap's fabricated identifiers."""
    idx = [i for i in _claims_of(ans, {"entity"})
           if ans["claims"][i].get("uids")]
    if not idx:
        return None
    i = rng.choice(idx)
    evidence_lexicon: set[str] = set()
    verifier = ctx["verifier"]
    payloads, _, _ = verifier._evidence_payloads(ans["claims"][i]["evidence"])
    for p in payloads:
        _collect_all_strings(p, evidence_lexicon)
    candidates = [u for u in ctx.get("uid_pool", [])
                  if u not in evidence_lexicon
                  and u not in ans["claims"][i]["uids"]]
    if not candidates:
        return None
    out = copy.deepcopy(ans)
    out["claims"][i]["uids"].append(rng.choice(candidates))
    return out


def ordering_swap(ans: dict[str, Any], rng: random.Random,
                  ctx: dict[str, Any]) -> dict[str, Any] | None:
    """Swap the operands of an ordering claim while keeping the relation."""
    idx = [i for i in _claims_of(ans, {"ordering"})
           if isinstance(ans["claims"][i].get("relation"), str)
           and ans["claims"][i]["relation"] != "equals"]
    if not idx:
        return None
    out = copy.deepcopy(ans)
    c = out["claims"][rng.choice(idx)]
    c["a"], c["b"] = c["b"], c["a"]
    return out


def interval_units(ans: dict[str, Any], rng: random.Random,
                   ctx: dict[str, Any]) -> dict[str, Any] | None:
    """Unit confusion: reinterpret microsecond endpoints as milliseconds
    (divide by 1000) — the classic wrong-unit / wrong-bucket error."""
    idx = _claims_of(ans, {"ordering", "temporal_pattern"})
    if not idx:
        return None
    out = copy.deepcopy(ans)
    c = out["claims"][rng.choice(idx)]
    key = "interval" if "interval" in c else rng.choice(["a", "b"])
    iv = c.get(key)
    if not isinstance(iv, dict):
        return None
    for k in ("t", "t_a", "t_b", "start", "end"):
        if k in iv:
            iv[k] = int(iv[k]) // 1000
    return out


def wrong_step_citation(ans: dict[str, Any], rng: random.Random,
                        ctx: dict[str, Any]) -> dict[str, Any] | None:
    """Keep the (correct) value but cite a different step's evidence."""
    step_ids = ctx.get("ok_step_ids") or []
    idx = [i for i, c in enumerate(ans["claims"])
           if c.get("evidence")
           and any(s not in c["evidence"] for s in step_ids)]
    if not idx or len(step_ids) < 2:
        return None
    out = copy.deepcopy(ans)
    c = out["claims"][rng.choice(idx)]
    others = [s for s in step_ids if s not in c["evidence"]]
    if not others:
        return None
    c["evidence"] = [rng.choice(others)]
    return out


EXT_MUTATORS: dict[str, ExtMutator] = {
    "entity_drop": entity_drop,
    "entity_add": entity_add,
    "ordering_swap": ordering_swap,
    "interval_units": interval_units,
    "wrong_step_citation": wrong_step_citation,
}


# ------------------------------------------------------------------ #
# generator classes: correct arithmetic, false statements            #
# ------------------------------------------------------------------ #

def _executor(store: Any, scratch: Any):
    from pathlib import Path

    from tgms.agent.executor import Executor, ResultStore
    from tgms.tools.server import ToolRouter

    results = ResultStore(Path(scratch) / "results")
    return Executor(ToolRouter(store.adapter), results), results


def truncated_count_cases(store: Any, suite: dict[str, Any],
                          scratch: Any, page: int = 3,
                          max_cases: int = 100) -> list[dict[str, Any]]:
    """Count tasks whose oracle plan counts rows of a paginated step: shrink
    that step's limit so the count runs over a truncated page. Arithmetic
    stays correct; the statement about the database becomes false.
    Each case records the verdict with taint honored and under the E2
    ablation (honor_truncation=False)."""
    from tgms.agent.ir import Plan
    from tgms.agent.reporter import mechanical_answer
    from tgms.tools.schemas import REGISTRY

    executor, results = _executor(store, scratch)
    cases = []
    for task in suite["dev"] + suite["test"]:
        if len(cases) >= max_cases:
            break
        if task.get("answer_kind") != "count" \
                or task.get("gold_source") != "oracle_plan":
            continue
        pj = copy.deepcopy(task["oracle_plan"])
        rows_steps = [st for st in pj["steps"]
                      if "limit" in st.get("args", {})
                      and "rows" in (getattr(REGISTRY.get(st["op"]), 
                                             "output_fields", ()) or ())]
        if not rows_steps:
            continue
        target = rows_steps[-1]
        target["args"]["limit"] = page
        try:
            trace = executor.run(Plan.from_json(pj))
        except Exception:
            continue
        if not trace.ok:
            continue
        full_total = None
        for rec in trace.steps:
            if rec["step_id"] == target["id"] and rec.get("status") == "ok":
                full_total = results.get(rec["result_digest"]).get("rows_total")
        if full_total is None or full_total <= page:
            continue  # nothing truncated -> not a case
        ans = mechanical_answer(Plan.from_json(pj), trace)
        if not ans["claims"]:
            continue
        v_taint = ClaimVerifier(trace, results, store.adapter)
        v_ablate = ClaimVerifier(trace, results, store.adapter,
                                 honor_truncation=False)
        cases.append({
            "task_id": task["id"],
            "page_count": trace.answer, "full_count": full_total,
            "verdict_with_taint":
                v_taint.verify(ans)["claims"][0]["verdict"],
            "verdict_without_taint":
                v_ablate.verify(ans)["claims"][0]["verdict"],
        })
    return cases


def wrong_belief_state_cases(store: Any, suite: dict[str, Any],
                             scratch: Any,
                             max_cases: int = 100) -> list[dict[str, Any]]:
    """For correction probes pinned to a past belief state, recompute the
    value under the CURRENT belief state and assert it against the pinned
    trace: correct number, wrong transaction time."""
    from tgms.agent.ir import Plan
    from tgms.agent.reporter import mechanical_answer

    def strip_tt(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: strip_tt(v) for k, v in obj.items() if k != "as_of_tt"}
        if isinstance(obj, list):
            return [strip_tt(v) for v in obj]
        return obj

    executor, results = _executor(store, scratch)
    cases = []
    for task in suite["dev"] + suite["test"]:
        if len(cases) >= max_cases:
            break
        if task.get("family") != "probe" \
                or task.get("gold_source") != "oracle_plan":
            continue
        pj = task["oracle_plan"]
        if '"as_of_tt"' not in __import__("json").dumps(pj):
            continue
        try:
            pinned = executor.run(Plan.from_json(pj))
            current = executor.run(Plan.from_json(strip_tt(pj)))
        except Exception:
            continue
        if not (pinned.ok and current.ok):
            continue
        if pinned.answer == current.answer:
            continue  # correction did not change this value; not a case
        ans = mechanical_answer(Plan.from_json(pj), pinned)
        claims = [c for c in ans["claims"]
                  if isinstance(c.get("value"), (int, float))]
        if not claims:
            continue
        mutant = copy.deepcopy(ans)
        for c in mutant["claims"]:
            if isinstance(c.get("value"), (int, float)):
                c["value"] = current.answer
                break
        verifier = ClaimVerifier(pinned, results, store.adapter)
        verdict = next(r["verdict"] for r in verifier.verify(mutant)["claims"]
                       if r["id"] == claims[0]["id"])
        cases.append({"task_id": task["id"],
                      "pinned_value": pinned.answer,
                      "current_value": current.answer,
                      "verdict": verdict})
    return cases


# ------------------------------------------------------------------ #
# driver                                                             #
# ------------------------------------------------------------------ #

def c2_extended_readout(store: Any, suite: dict[str, Any],
                        per_class: int = 100, seed: int = 0,
                        scratch_dir: str | None = None) -> dict[str, Any]:
    """Per-class detection table over the extended mutation classes plus
    the two generator classes. FP rate is measured on the enriched clean
    pool (mechanical answers + factory ordering/pattern claims that verify
    as supported)."""
    import tempfile
    from pathlib import Path

    from tgms.agent.executor import Executor, ResultStore
    from tgms.agent.ir import Plan
    from tgms.agent.reporter import mechanical_answer
    from tgms.tools.server import ToolRouter

    scratch = Path(scratch_dir or tempfile.mkdtemp(prefix="tgms-c2x-"))
    results = ResultStore(scratch / "results")
    executor = Executor(ToolRouter(store.adapter), results)
    rng = random.Random(seed)

    # clean pool: mechanical answers enriched with factory claims
    pool: list[tuple[dict[str, Any], ClaimVerifier, dict[str, Any]]] = []
    uid_pool: set[str] = set()
    for task in suite["dev"] + suite["test"]:
        if task.get("gold_source") != "oracle_plan":
            continue
        try:
            trace = executor.run(Plan.from_json(task["oracle_plan"]))
        except Exception:
            continue
        if not trace.ok:
            continue
        ans = mechanical_answer(Plan.from_json(task["oracle_plan"]), trace)
        verifier = ClaimVerifier(trace, results, store.adapter)
        for claim in factory_ordering(trace, results) \
                + factory_pattern(trace, results):
            probe = {"text": ans["text"], "claims": [claim]}
            if verifier.verify(probe)["claims"][0]["verdict"] == "supported":
                ans["claims"].append(claim)
        if not ans["claims"]:
            continue
        report = verifier.verify(ans)
        if not all(r["verdict"] in ("supported", "weakly_supported")
                   for r in report["claims"]):
            continue
        lex: set[str] = set()
        for rec in trace.steps:
            if rec.get("status") == "ok":
                _collect_all_strings(results.get(rec["result_digest"]), lex)
        uid_pool |= {s for s in lex if s.startswith("n") and s[1:].isdigit()}
        ctx = {"verifier": verifier,
               "ok_step_ids": [r["step_id"] for r in trace.steps
                               if r.get("status") == "ok"]}
        pool.append((ans, verifier, ctx))

    def rejected(report: dict[str, Any]) -> bool:
        return any(r["verdict"] in ("unsupported", "unverifiable")
                   for r in report["claims"])

    fp = sum(rejected(v.verify(a)) for a, v, _ in pool)

    per: dict[str, dict[str, Any]] = {}
    for name, fn in EXT_MUTATORS.items():
        made = detected = 0
        applicable = []
        probe_rng = random.Random(seed ^ 0x5EED)
        for i, (ans, v, ctx) in enumerate(pool):
            ctx2 = {**ctx, "uid_pool": sorted(uid_pool)}
            if fn(ans, probe_rng, ctx2) is not None:
                applicable.append(i)
        while made < per_class and applicable:
            i = applicable[rng.randrange(len(applicable))]
            ans, v, ctx = pool[i]
            mutant = fn(ans, rng, {**ctx, "uid_pool": sorted(uid_pool)})
            if mutant is None:
                continue
            made += 1
            if rejected(v.verify(mutant)):
                detected += 1
        per[name] = {"made": made, "detected": detected,
                     "rate": round(detected / made, 3) if made else None}

    tc = truncated_count_cases(store, suite, scratch, max_cases=per_class)
    per["truncated_count"] = {
        "made": len(tc),
        "detected": sum(1 for c in tc
                        if c["verdict_with_taint"] != "supported"),
        "rate": round(sum(1 for c in tc
                          if c["verdict_with_taint"] != "supported")
                      / len(tc), 3) if tc else None,
        "ablation_miss": sum(1 for c in tc
                             if c["verdict_without_taint"] == "supported"),
    }
    wb = wrong_belief_state_cases(store, suite, scratch, max_cases=per_class)
    per["wrong_belief_state"] = {
        "made": len(wb),
        "detected": sum(1 for c in wb if c["verdict"] != "supported"),
        "rate": round(sum(1 for c in wb if c["verdict"] != "supported")
                      / len(wb), 3) if wb else None,
    }

    return {"n_clean_answers": len(pool), "false_positives": fp,
            "fp_rate": round(fp / len(pool), 4) if pool else None,
            "per_class": per}
