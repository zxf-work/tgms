"""The trust-boundary fault matrix (Lane E, task E1).

NORMATIVE: `docs/design/TRUST_BOUNDARY_FAULT_MATRIX_DESIGN_2026-09-13.md`
(FROZEN). This script drives `tgms.eval.plan_faults`'s cell registry,
mutators and classifier over real plans and a real store -- **model-free**
(§5's primary arm): every plan mutated here is a frozen suite's own
`oracle_plan`, run through `Executor`/`ToolRouter`/`mechanical_answer`
(LLM-free, deterministic), never an LLM-produced plan. No network call, no
LLM call, anywhere in this file.

Usage:

    uv run python scripts/eval_trust_boundary_matrix.py --dry-run
    uv run python scripts/eval_trust_boundary_matrix.py --suite collegemsg \\
        --cells F1-1,F1-3a --n 2 --seed 0 --store stores/collegemsg \\
        --out benchmarks/faults-v1

`--cells` (alias `--mutators`, matching §6's literal CLI spelling) accepts a
comma list of cell ids from `tgms.eval.plan_faults.CELL_REGISTRY`, or `all`.
`--surface` restricts to `A`, `B`, or `A,B` (default: whatever each
selected cell's own registry entry declares runnable).

**Gating arms (coordinator ruling, Addendum 1 to the frozen design,
2026-09-13).** The default (primary) arm classifies the answer the
deployed "ours" system actually delivers: `gate_answer` drops exactly what
`tgms.eval.harness.run_task_ours`'s own gate drops (`unsupported` only).
An emitted `unverifiable` claim the evidence does not support therefore
reaches `classify` and is a real `I1` `silent-violation` -- the finding
the matrix exists to surface, not something pre-empted by widening the
gate. `--strict-gate` is a named secondary arm (drops `unsupported` and
`unverifiable`), written to `<cell>-<suite>-strict.json` so it never
overwrites the primary-arm record. `weakly_supported` claims are never
reclassified either way (§4 stays verbatim, outcome `correct`); each
trial and the per-cell table instead carry a `weak_support` flag so the
fact is visible without inventing a fifth outcome.

**A named interpretive gap, not a silent one** (see the module docstring of
`tgms.eval.plan_faults` and the E1 report): §5 pre-registers "N = 300
mutants per mutator for the eight cells with an existing detection
mechanism ... and N = 100 for the remainder," but does not enumerate the
eight by cell id. `EIGHT_EXISTING_MACHINERY_CELLS` below is this script's
reading -- the cells whose detection mechanism (verifier claim gating,
truncation taint, the O-LATTICE refusal, or the executor's own failure
isolation) predates this task, as opposed to the F1-1/2/3/4/5/6 static
cells and the F2 injectors, which are new machinery this task builds.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import platform
import random
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tgms  # noqa: E402
from tgms.agent.executor import Executor, ResultStore  # noqa: E402
from tgms.agent.ir import Plan  # noqa: E402
from tgms.agent.reporter import mechanical_answer  # noqa: E402
from tgms.agent.verifier import ClaimVerifier, validate_static  # noqa: E402
from tgms.core.errors import TgmsError  # noqa: E402
from tgms.core.model import canonical_json, sha256_hex  # noqa: E402
from tgms.tools.server import ToolRouter  # noqa: E402
from tgms.eval.plan_faults import (  # noqa: E402
    EXEC_FAULTS, EXT_MUTATORS, MUTATORS, PLAN_MUTATORS, TGIR_MUTATORS,
    CELL_REGISTRY, Classification, Oracle, Outcome, Run, augment_report,
    classify, f2_7_stale_index_metadata_by_reference, gate_answer,
    has_weak_support, make_misattribution_oracle, tiny_cost_ceilings,
    to_certificate, truncated_count_cases,
)
from tgms.tools.retry_io import mkdir_with_retry, write_bytes_with_retry  # noqa: E402

#: This script's reading of §5's "eight cells with an existing detection
#: mechanism" -- see the module docstring above.
EIGHT_EXISTING_MACHINERY_CELLS: frozenset[str] = frozenset({
    "F1-7", "F1-8", "F1-9", "F1-10", "F1-11", "F2-1", "F2-4", "F2-6",
})

DEFAULT_N_EXISTING = 300
DEFAULT_N_OTHER = 100

FROZEN_LOG = ROOT / "benchmarks/frozen-v1/collegemsg.eventlog.jsonl"
TGIR_PLANS_DIR = ROOT / "benchmarks/tgir-v1/plans"

#: Cells this driver can execute end to end without an LLM. F2-7 is
#: excluded on purpose (§3: reused by reference, never rerun); F1-6's
#: oracled sub-case rides under its own key, `F1-6b` (see
#: `tgms.eval.plan_faults`'s module docstring) and is offered here too even
#: though it is not one of the frozen table's 19 rows.
RUNNABLE_CELLS: tuple[str, ...] = tuple(
    c for c in list(CELL_REGISTRY) + ["F1-6b"] if c != "F2-7")


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True,
                              check=True, cwd=ROOT).stdout.strip()
    except Exception:
        return "n/a"


# --------------------------------------------------------------------------- #
# store / suite / plan loading                                                #
# --------------------------------------------------------------------------- #

def ensure_collegemsg_store(path: Path) -> None:
    """Build `stores/collegemsg` by replaying the frozen event log, the
    same recipe `benchmarks/frozen-v1/README.md` and `scripts/eval_guardrail
    .py:build_store` use -- never a fresh `ingest` (D-023: transaction
    times would be reassigned)."""
    from tgms.storage.eventlog import replay

    if path.exists():
        return
    if not FROZEN_LOG.exists():
        raise FileNotFoundError(
            f"no store at {path} and no frozen event log at {FROZEN_LOG} to "
            "build one from")
    path.parent.mkdir(parents=True, exist_ok=True)
    store = tgms.open(path, backend="native")
    replay(FROZEN_LOG, store.adapter)
    store.close()


def resolve_store(name_or_path: str):
    path = Path(name_or_path)
    if not path.is_absolute():
        path = ROOT / path
    if path.name == "collegemsg" or name_or_path == "collegemsg":
        ensure_collegemsg_store(path)
    if not path.exists():
        raise FileNotFoundError(
            f"store {path} does not exist and this driver only knows how "
            "to auto-build 'collegemsg' (from the frozen event log); build "
            "others with the dataset's own script "
            "(e.g. scripts/build_ldbc_fixture.py for surface B) and pass "
            "--store explicitly")
    return tgms.open(path, backend="native")


def load_suite(name_or_path: str) -> dict[str, Any]:
    path = Path(name_or_path)
    if not path.exists():
        path = ROOT / "benchmarks/frozen-v1" / f"suite-{name_or_path}.json"
    return json.loads(path.read_text())


def oracle_tasks(suite: dict[str, Any]) -> list[dict[str, Any]]:
    return [t for t in suite["dev"] + suite["test"]
            if t.get("gold_source") == "oracle_plan"]


def runnable_tgir_plans() -> list[dict[str, Any]]:
    out = []
    for f in sorted(TGIR_PLANS_DIR.glob("*.json")):
        doc = json.loads(f.read_text())
        if doc.get("root") is not None:
            out.append(doc)
    return out


def build_executor(store: Any, scratch: Path,
                   router: Any | None = None) -> tuple[Executor, ResultStore]:
    results = ResultStore(scratch / "results")
    rt = router if router is not None else ToolRouter(store.adapter, tt_source=store)
    return Executor(rt, results), results


# --------------------------------------------------------------------------- #
# surface A: plan-DAG mutation trials (F1-1 .. F1-6b, F1-3b included)         #
# --------------------------------------------------------------------------- #

def gated_run(answer_obj: dict[str, Any], trace: Any, verifier: ClaimVerifier,
             oracle: Oracle, strict: bool = False) -> tuple[Classification, bool]:
    """The delivered-answer reading (§2's "(gated)" annotations): verify,
    gate exactly as the deployed 'ours' system does by default
    (`gate_answer`; coordinator Addendum 1 -- `strict=True` is the named
    `--strict-gate` secondary arm), and classify what actually reaches the
    user. A claim gated to nothing (the only claim, dropped) is a failure
    to answer, not a vacuously-correct empty answer -- represented as
    `Run(error=...)` so `classify` takes its `explicit-failure` branch.

    Returns `(classification, weak_support)` -- Addendum 1's visibility
    flag (`has_weak_support`), never folded into the classification itself.
    """
    report = verifier.verify(answer_obj)
    aug = augment_report(answer_obj, report, verifier)
    gated, gated_report, n_dropped = gate_answer(answer_obj, aug, strict=strict)
    weak_support = has_weak_support(gated_report)
    if not gated["claims"] and n_dropped:
        err = {"error": "E_GATED_EMPTY",
              "message": "every claim was gated out before delivery",
              "details": {"n_dropped": n_dropped}}
        return classify(Run(error=err), oracle), weak_support
    run = Run(answer=gated, answer_value=trace.answer, report=gated_report)
    return classify(run, oracle), weak_support


def run_agent_plan_trial(plan_json: dict[str, Any], task_input_uids: set[str],
                         gold: Any, store: Any, scratch: Path,
                         mutator: Callable[..., Any | None],
                         rng: random.Random,
                         strict: bool = False) -> dict[str, Any] | None:
    """One trial of a `PLAN_MUTATORS` cell: mutate, statically validate,
    execute if valid, mechanically answer and verify if it executes --
    every stage LLM-free. Returns `None` when the mutator declined (the
    §6 "not applicable" convention), so the caller can retry a different
    plan without spending an attempt budget on nothing."""
    mutant = mutator(plan_json, rng, {"stats": store.adapter.stats()})
    if mutant is None:
        return None

    static = validate_static(mutant, adapter=store.adapter,
                             task_input_uids=task_input_uids)
    if not static["valid"]:
        v = static["violations"][0]
        err = {"error": v["code"], "message": v["message"], "details": {}}
        run = Run(error=err, certificate=to_certificate(err))
        cls = classify(run, Oracle(gold=gold))
        return {"stage": "static", "mutant": mutant, **cls.to_json()}

    executor, results = build_executor(store, scratch)
    try:
        plan = Plan.from_json(mutant)
    except Exception as e:  # a plan validate_static passed but Plan cannot
        err = {"error": "E_SCHEMA", "message": str(e), "details": {}}
        cls = classify(Run(error=err), Oracle(gold=gold))
        return {"stage": "parse", "mutant": mutant, **cls.to_json()}

    trace = executor.run(plan)
    if not trace.ok:
        errs = [s.get("error") for s in trace.steps if s.get("status") != "ok"]
        err = (errs[0] if errs else
              {"error": "E_ANSWER", "message": str(trace.answer_error), "details": {}})
        run = Run(error=err, certificate=to_certificate(err))
        cls = classify(run, Oracle(gold=gold))
        return {"stage": "trace", "mutant": mutant, **cls.to_json()}

    ans = mechanical_answer(plan, trace)
    verifier = ClaimVerifier(trace, results, store.adapter)
    cls, weak_support = gated_run(ans, trace, verifier, Oracle(gold=gold), strict=strict)
    return {"stage": "answer", "mutant": mutant, "weak_support": weak_support,
           **cls.to_json()}


def run_plan_cell(cell_id: str, mutator: Callable[..., Any | None],
                  tasks: list[dict[str, Any]], store: Any, scratch: Path,
                  n: int, seed: int, strict: bool = False) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    trials: list[dict[str, Any]] = []
    attempts = 0
    max_attempts = max(50, n * 25)
    while len(trials) < n and attempts < max_attempts:
        attempts += 1
        task = tasks[rng.randrange(len(tasks))]
        out = run_agent_plan_trial(task["oracle_plan"], set(task["input_uids"]),
                                   task.get("gold"), store, scratch, mutator, rng,
                                   strict=strict)
        if out is not None:
            out["task_id"] = task["id"]
            trials.append(out)
    return trials


# --------------------------------------------------------------------------- #
# surface A: reused answer-level cells (F1-7, F1-9, F1-10, F1-11)             #
# --------------------------------------------------------------------------- #

#: §2's "Reuse, do not reimplement" mapping from cell id to the pre-existing
#: mutator it plays back. F1-7 and F1-10 are both read off the same
#: `count_pm1`/`uid_swap` pool the C2 500/500 acceptance number comes from
#: (§8: "F1-7/10 -> >= 0.95, matching the existing 500/500") -- the memo
#: does not name a mutator distinct from F1-10's for F1-7, so this driver
#: alternates both value- and identity-fabrication mutators for each.
ANSWER_LEVEL_MUTATOR_NAMES: dict[str, tuple[str, ...]] = {
    "F1-7": ("count_pm1",),
    "F1-9": ("wrong_step_citation",),
    "F1-10": ("count_pm1", "uid_swap"),
}
_ANSWER_MUTATORS: dict[str, Callable[..., Any | None]] = {**MUTATORS, **EXT_MUTATORS}


def _clean_answer_pool(tasks: list[dict[str, Any]], store: Any,
                       scratch: Path) -> list[tuple[dict, ClaimVerifier, Any, Any]]:
    executor, results = build_executor(store, scratch)
    pool = []
    for task in tasks:
        try:
            trace = executor.run(Plan.from_json(task["oracle_plan"]))
        except Exception:
            continue
        if not trace.ok:
            continue
        ans = mechanical_answer(Plan.from_json(task["oracle_plan"]), trace)
        if not ans["claims"]:
            continue
        verifier = ClaimVerifier(trace, results, store.adapter)
        report = verifier.verify(ans)
        if not all(r["verdict"] in ("supported", "weakly_supported")
                  for r in report["claims"]):
            continue
        pool.append((ans, verifier, task.get("gold"), trace))
    return pool


def run_answer_level_cell(cell_id: str, tasks: list[dict[str, Any]], store: Any,
                          scratch: Path, n: int, seed: int,
                          strict: bool = False) -> list[dict[str, Any]]:
    if cell_id == "F1-11":
        cases = truncated_count_cases(store, {"dev": [], "test": tasks},
                                      scratch, max_cases=n)
        out = []
        for c in cases:
            verdict = c["verdict_with_taint"]
            report = {"claims": [{"verdict": verdict, "reason": "taint-capped",
                                  "truncated": verdict == "weakly_supported",
                                  "cited_uids": [], "cited_values": [c["page_count"]]}]}
            answer = {"claims": [{"id": "c1", "type": "count",
                                  "value": c["page_count"], "evidence": ["s1"]}]}
            run = Run(answer=answer, answer_value=c["page_count"], report=report)
            cls = classify(run, Oracle(gold=c["full_count"]))
            out.append({"stage": "answer", "task_id": c["task_id"],
                       "weak_support": has_weak_support(report), **cls.to_json()})
        return out

    pool = _clean_answer_pool(tasks, store, scratch)
    if not pool:
        return []
    names = ANSWER_LEVEL_MUTATOR_NAMES[cell_id]
    rng = random.Random(seed)
    trials: list[dict[str, Any]] = []
    attempts = 0
    max_attempts = max(50, n * 25)
    while len(trials) < n and attempts < max_attempts:
        attempts += 1
        ans, verifier, gold, trace = pool[rng.randrange(len(pool))]
        name = names[rng.randrange(len(names))]
        fn = _ANSWER_MUTATORS[name]
        ctx = {"verifier": verifier,
              "ok_step_ids": [r["step_id"] for r in trace.steps
                             if r.get("status") == "ok"],
              "uid_pool": []}
        try:
            mutant = fn(ans, rng, ctx) if fn in EXT_MUTATORS.values() else fn(ans, rng)
        except TypeError:
            mutant = fn(ans, rng)
        if mutant is None:
            continue
        oracle = Oracle(gold=gold, cited_step_wrong=make_misattribution_oracle(ans["claims"]))
        cls, weak_support = gated_run(mutant, trace, verifier, oracle, strict=strict)
        trials.append({"stage": "answer", "mutator": name, "weak_support": weak_support,
                      **cls.to_json()})
    return trials


def f1_8_rows_steps(plan_json: dict[str, Any]) -> list[dict[str, Any]]:
    """The F1-8 candidate filter: steps carrying a `limit` arg whose
    operator declares `rows` among its output fields. Calls
    `ensure_all_registered()` first: `REGISTRY` is populated lazily by
    importing the `tgms.temporal.ops_*` modules, and this filter is the
    one registry consumer in the driver with no `ToolRouter`,
    `validate_static` or mutator ahead of it to have done so. Without
    the call, `--cells F1-8` alone saw an empty registry and produced
    0 trials while the same cell after F1-6b in one process produced
    trials (2026-09-14; the 2026-09-13 campaign's F1-8 row, 0/300, is
    this bug)."""
    from tgms.temporal.algebra import REGISTRY, ensure_all_registered

    ensure_all_registered()
    return [st for st in plan_json["steps"]
            if "limit" in st.get("args", {})
            and "rows" in (getattr(REGISTRY.get(st["op"]),
                                   "output_fields", ()) or ())]


def run_f1_8_cell(tasks: list[dict[str, Any]], store: Any, scratch: Path,
                  n: int, seed: int) -> list[dict[str, Any]]:
    """F1-8, "unsupported aggregation": shrink a paginated rows-step's
    `limit` aggressively (to 1) and require the *executor itself* to
    refuse -- the REDUCING_FNS/`upstream_truncated` guard
    (`tgms/agent/executor.py:204-224`) firing before an answer, the
    A-side analogue of TGIR's O-LATTICE incompleteness refusal. A task
    whose oracle plan does not have a reducing `compute` step consuming a
    paginated step's rows never reaches this shape and is skipped (not
    counted as a trial), mirroring `truncated_count_cases`'s own
    plan-selection logic (`faults_ext.py:223-262`) but requiring the
    opposite outcome: that function keeps only the cases where truncation
    reaches a *claim* (F1-11's shape); this one keeps only the cases where
    it is refused before one exists."""
    rng = random.Random(seed)
    cands = [t for t in tasks if t.get("gold_source") == "oracle_plan"]
    trials: list[dict[str, Any]] = []
    attempts = 0
    max_attempts = max(50, n * 40)
    while len(trials) < n and attempts < max_attempts and cands:
        attempts += 1
        task = cands[rng.randrange(len(cands))]
        pj = copy.deepcopy(task["oracle_plan"])
        rows_steps = f1_8_rows_steps(pj)
        if not rows_steps:
            continue
        rows_steps[-1]["args"]["limit"] = 1
        executor, results = build_executor(store, scratch)
        try:
            trace = executor.run(Plan.from_json(pj))
        except Exception:
            continue
        if trace.ok:
            continue  # this plan's shape does not force the refusal
        errs = [s.get("error") for s in trace.steps if s.get("status") != "ok"]
        err = (errs[0] if errs else
              {"error": "E_ANSWER", "message": str(trace.answer_error), "details": {}})
        run = Run(error=err, certificate=to_certificate(err))
        cls = classify(run, Oracle(gold=task.get("gold")))
        trials.append({"stage": "trace", "task_id": task["id"], **cls.to_json()})
    return trials


# --------------------------------------------------------------------------- #
# surface A: F2 execution faults                                              #
# --------------------------------------------------------------------------- #

def run_exec_fault_cell(cell_id: str, tasks: list[dict[str, Any]], store: Any,
                        scratch: Path, n: int, seed: int,
                        strict: bool = False) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    trials: list[dict[str, Any]] = []
    attempts = 0
    max_attempts = max(50, n * 10)
    while len(trials) < n and attempts < max_attempts:
        attempts += 1
        task = tasks[rng.randrange(len(tasks))]
        plan_json = task["oracle_plan"]
        n_steps = max(1, len(plan_json["steps"]))
        target_call = rng.randrange(1, n_steps + 1)

        if cell_id == "F2-5":
            router = ToolRouter(store.adapter, tt_source=store,
                               cost_ceilings=tiny_cost_ceilings())
        else:
            inner = ToolRouter(store.adapter, tt_source=store)
            router = EXEC_FAULTS[cell_id](inner, target_call, rng)

        executor, results = build_executor(store, scratch, router=router)
        try:
            trace = executor.run(Plan.from_json(plan_json))
        except Exception as e:
            err = {"error": "E_INTERNAL", "message": str(e), "details": {}}
            cls = classify(Run(error=err), Oracle(gold=task.get("gold")))
            trials.append({"stage": "trace", "task_id": task["id"], **cls.to_json()})
            continue

        if not trace.ok:
            errs = [s.get("error") for s in trace.steps if s.get("status") != "ok"]
            err = (errs[0] if errs else
                  {"error": "E_ANSWER", "message": str(trace.answer_error), "details": {}})
            run = Run(error=err, certificate=to_certificate(err))
            cls = classify(run, Oracle(gold=task.get("gold")))
            trials.append({"stage": "trace", "task_id": task["id"],
                          "injected": getattr(router, "injected", None), **cls.to_json()})
            continue

        ans = mechanical_answer(Plan.from_json(plan_json), trace)
        verifier = ClaimVerifier(trace, results, store.adapter)
        cls, weak_support = gated_run(ans, trace, verifier, Oracle(gold=task.get("gold")),
                                      strict=strict)
        if cell_id == "F2-3" and getattr(router, "true_rows_total", None) is not None:
            # §3, F2-3: undetectable by construction (assumption A2) -- the
            # pipeline under test cannot see this, but this harness
            # constructed the fault and knows the true row count, so it
            # overrides classify()'s trace-relative verdict rather than
            # reporting a false `correct`. Excluded from the headline count
            # (CELL_REGISTRY["F2-3"].headline is False).
            cls = Classification(Outcome.SILENT_VIOLATION,
                                 reason="assumption-A2-exercised (silent "
                                        "partial result, undetectable by "
                                        "construction)",
                                 gold_mismatch=cls.gold_mismatch,
                                 misattributed=cls.misattributed)
        trials.append({"stage": "answer", "task_id": task["id"],
                      "injected": getattr(router, "injected", None),
                      "true_rows_total": getattr(router, "true_rows_total", None),
                      "weak_support": weak_support, **cls.to_json()})
    return trials


# --------------------------------------------------------------------------- #
# surface B: TGIR plan mutation                                               #
# --------------------------------------------------------------------------- #

def run_tgir_cell(cell_id: str, mutator: Callable[..., Any | None],
                  plans: list[dict[str, Any]], store: Any, n: int,
                  seed: int) -> list[dict[str, Any]]:
    from tgms.tgir import loader as tgir_loader
    from tgms.tgir.execute import run_plan as tgir_run_plan

    rng = random.Random(seed)
    trials: list[dict[str, Any]] = []
    attempts = 0
    max_attempts = max(50, n * 25)
    while len(trials) < n and attempts < max_attempts:
        attempts += 1
        doc = plans[rng.randrange(len(plans))]
        ctx = {"sigma": doc.get("sigma")}
        mutant_root = mutator(doc["root"], rng, ctx)
        if mutant_root is None:
            continue
        mutant_doc = {**doc, "root": mutant_root}
        try:
            root = tgir_loader.load(mutant_doc)
            envelope = tgir_run_plan(root, store.adapter, plan_id=doc["plan_id"],
                                     tt_source=store)
        except Exception as e:
            err = ({"error": e.code, "message": e.message, "details": e.details}
                  if isinstance(e, TgmsError) else
                  {"error": type(e).__name__, "message": str(e), "details": {}})
            run = Run(error=err, certificate=to_certificate(err))
            cls = classify(run, Oracle())
            trials.append({"plan_id": doc["plan_id"], **cls.to_json()})
            continue

        run = Run(answer={"claims": []}, answer_value=envelope.get("result_digest"))
        cls = classify(run, Oracle())
        trials.append({"plan_id": doc["plan_id"],
                      "result_digest": envelope.get("result_digest"), **cls.to_json()})
    return trials


# --------------------------------------------------------------------------- #
# driver                                                                       #
# --------------------------------------------------------------------------- #

def default_n(cell_id: str) -> int:
    return DEFAULT_N_EXISTING if cell_id in EIGHT_EXISTING_MACHINERY_CELLS else DEFAULT_N_OTHER


def outcome_table(trials: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {o.value: 0 for o in Outcome}
    gold_mismatch = 0
    misattributed = 0
    weak_support = 0
    for t in trials:
        counts[t["outcome"]] = counts.get(t["outcome"], 0) + 1
        gold_mismatch += bool(t.get("gold_mismatch"))
        misattributed += bool(t.get("misattributed"))
        weak_support += bool(t.get("weak_support"))
    n = len(trials)
    return {"n": n, "counts": counts, "gold_mismatch": gold_mismatch,
           "misattributed": misattributed,
           # Addendum 1: visible, never folded into `counts` -- a `correct`
           # trial with weak_support=True is still counted as `correct`.
           "weak_support": weak_support}


def false_refusal_rate(control_trials: list[dict[str, Any]]) -> float | None:
    if not control_trials:
        return None
    refused = sum(1 for t in control_trials if t["outcome"] == Outcome.SAFE_REFUSAL.value)
    return refused / len(control_trials)


def run_cell(cell_id: str, suite: dict[str, Any], store: Any, scratch: Path,
            n: int, seed: int, tgir_plans: list[dict[str, Any]] | None,
            surface: str | None = None, strict: bool = False) -> list[dict[str, Any]]:
    """`surface`, when given, picks between a cell id that both
    `PLAN_MUTATORS` and `TGIR_MUTATORS` register (F1-1, F1-3a, F1-3b,
    F1-5): "B" routes to the TGIR driver, anything else (including
    unset) keeps the surface-A default. Without this, a surface-B-only
    invocation would be unreachable, since every `TGIR_MUTATORS` key also
    exists in `PLAN_MUTATORS`.

    `strict` selects the coordinator's Addendum 1 secondary arm
    (`--strict-gate`: `gate_answer(..., strict=True)`, dropping
    `unverifiable` claims too) in every gated driver; it is a no-op for
    cells with no answer/gate at all (F1-8, F2-5, surface B)."""
    if cell_id == "F2-7":
        f2_7_stale_index_metadata_by_reference()  # always raises -- never run
    if surface == "B" and cell_id in TGIR_MUTATORS:
        if not tgir_plans:
            raise ValueError(f"cell {cell_id}: surface B requested but no "
                             "runnable TGIR plans were loaded")
        return run_tgir_cell(cell_id, TGIR_MUTATORS[cell_id], tgir_plans, store, n, seed)
    tasks = oracle_tasks(suite)
    if cell_id == "F1-8":
        return run_f1_8_cell(tasks, store, scratch, n, seed)
    if cell_id in PLAN_MUTATORS:
        return run_plan_cell(cell_id, PLAN_MUTATORS[cell_id], tasks, store, scratch, n,
                             seed, strict=strict)
    if cell_id in ANSWER_LEVEL_MUTATOR_NAMES or cell_id == "F1-11":
        return run_answer_level_cell(cell_id, tasks, store, scratch, n, seed, strict=strict)
    if cell_id in EXEC_FAULTS or cell_id == "F2-5":
        return run_exec_fault_cell(cell_id, tasks, store, scratch, n, seed, strict=strict)
    if cell_id in TGIR_MUTATORS:
        if not tgir_plans:
            raise ValueError(f"cell {cell_id} is surface-B only; pass TGIR plans")
        return run_tgir_cell(cell_id, TGIR_MUTATORS[cell_id], tgir_plans, store, n, seed)
    raise ValueError(f"cell {cell_id} has no runnable driver in this script")


def build_manifest(cell_id: str, suite_name: str, suite_path: Path, seed: int,
                   n: int, trials: list[dict[str, Any]],
                   strict: bool = False) -> dict[str, Any]:
    cell = CELL_REGISTRY.get(cell_id)
    protocol = ("model-free (secondary arm, --strict-gate, coordinator "
               "Addendum 1 2026-09-13): oracle plans mutated post-planner, "
               "no LLM, unsupported+unverifiable both gated" if strict else
               "model-free (primary arm, section 5; gate matches "
               "tgms.eval.harness.run_task_ours exactly per coordinator "
               "Addendum 1 2026-09-13): oracle plans mutated post-planner, "
               "no LLM")
    return {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "git_commit": _git("rev-parse", "HEAD"),
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "machine": {"host": socket.gethostname(), "platform": platform.platform(),
                   "cpus": os.cpu_count()},
        "seed": seed,
        "dataset": {"name": suite_name, "digest": sha256_hex(suite_path.read_text())
                   if suite_path.exists() else None},
        "protocol": protocol,
        "strict_gate": strict,
        "surface": cell.surface if cell else "?",
        "cell": cell_id,
        "n_requested": n,
        "n_cases": len(trials),
        "outcomes": outcome_table(trials),
        "false_refusal_rate": false_refusal_rate(
            [t for t in trials if t.get("stage") == "static"]) if trials else None,
        "per_case": trials,
        "result_digest": sha256_hex(canonical_json(trials)),
    }


def parse_cells(spec: str) -> list[str]:
    if spec == "all":
        return list(RUNNABLE_CELLS)
    return [c.strip() for c in spec.split(",") if c.strip()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--suite", default="collegemsg")
    ap.add_argument("--surface", default=None, help="A, B, or A,B (default: "
                    "whatever each selected cell's registry entry declares)")
    ap.add_argument("--cells", "--mutators", dest="cells", default="all")
    ap.add_argument("--n", type=int, default=None)
    ap.add_argument("--seed", "--seeds", dest="seed", type=int, default=0)
    ap.add_argument("--store", default="stores/collegemsg")
    ap.add_argument("--out", default="benchmarks/faults-v1")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--strict-gate", action="store_true",
                    help="Secondary arm (coordinator Addendum 1, 2026-09-13): "
                         "gate_answer drops unsupported AND unverifiable "
                         "claims before classification, instead of matching "
                         "harness.py::run_task_ours's production gate "
                         "(unsupported only, the default/primary arm).")
    args = ap.parse_args(argv)

    cells = parse_cells(args.cells)
    if args.dry_run:
        for c in cells:
            n = args.n if args.n is not None else default_n(c)
            cell = CELL_REGISTRY.get(c)
            print(f"{c}\tn={n}\tsurface={cell.surface if cell else '?'}\t"
                 f"{cell.fault if cell else '(F1-6b: oracled sub-case of F1-6)'}")
        return 0

    suite_path = ROOT / "benchmarks/frozen-v1" / f"suite-{args.suite}.json"
    suite = load_suite(args.suite)
    store = resolve_store(args.store)
    tgir_plans = runnable_tgir_plans() if any(c in TGIR_MUTATORS for c in cells) else None

    out_dir = ROOT / args.out
    mkdir_with_retry(out_dir)
    with tempfile.TemporaryDirectory(prefix="tgms-faultmatrix-") as tmp:
        scratch = Path(tmp)
        for c in cells:
            n = args.n if args.n is not None else default_n(c)
            try:
                trials = run_cell(c, suite, store, scratch, n, args.seed, tgir_plans,
                                 surface=args.surface, strict=args.strict_gate)
            except ValueError as e:
                print(f"{c}: SKIPPED -- {e}")
                continue
            manifest = build_manifest(c, args.suite, suite_path, args.seed, n, trials,
                                      strict=args.strict_gate)
            suffix = "-strict" if args.strict_gate else ""
            out_path = out_dir / f"{c}-{args.suite}{suffix}.json"
            write_bytes_with_retry(out_path, canonical_json(manifest).encode("utf-8"))
            t = outcome_table(trials)
            print(f"{c}\tn={t['n']}\t{t['counts']}\tgold_mismatch={t['gold_mismatch']}\t"
                 f"misattributed={t['misattributed']}\tweak_support={t['weak_support']}\t"
                 f"-> {out_path}")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
