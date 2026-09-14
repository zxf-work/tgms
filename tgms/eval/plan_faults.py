"""Plan-level and execution-level fault injection (Lane E, task E1).

Implements, and does not re-decide, `docs/design/
TRUST_BOUNDARY_FAULT_MATRIX_DESIGN_2026-09-13.md` (FROZEN 2026-09-13): §2's
F1 planner/agent faults, §3's F2 execution faults, and §4's four-way outcome
classifier. Every cell number below (`F1-1` .. `F1-11`, `F2-1` .. `F2-7`)
names a row of that memo's tables; `CELL_REGISTRY` transcribes them so a
later edit to the frozen memo shows up as a test failure here rather than a
silent drift (see `tests/test_plan_faults.py`).

Two injection surfaces (§1): **A**, the agent plan-DAG (`tgms.agent.ir.Plan`,
`Plan.to_json()`), and **B**, TGIR plans (`benchmarks/tgir-v1/plans/*.json`,
mutated over the `root` node dict). Reuse, do not reimplement, per §2's own
instruction: the answer-level cells (F1-7, F1-8 (A-analogue), F1-9, F1-10,
F1-11) are `tgms.eval.faults.MUTATORS`, `tgms.eval.faults_ext.EXT_MUTATORS`,
`truncated_count_cases` and `wrong_belief_state_cases`, imported and
re-exported below, never rewritten.

No LLM calls anywhere in this module (§5's model-free primary arm).
"""

from __future__ import annotations

import copy
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable

import jsonschema

from tgms.core.errors import InternalError, NotFoundError, TgmsError
from tgms.agent.verifier import FLOAT_TOL, UID_ARG_KEYS

# --------------------------------------------------------------------------- #
# §6: "Import and re-export ... for the answer-level cells." Reuse, not      #
# reimplementation.                                                           #
# --------------------------------------------------------------------------- #
from tgms.eval.faults import (  # noqa: F401
    MUTATORS, c2_readout_from_suite, run_fault_injection,
)
from tgms.eval.faults_ext import (  # noqa: F401
    EXT_MUTATORS, truncated_count_cases, wrong_belief_state_cases,
)

Ctx = dict[str, Any]
#: §6's literal type: "`PlanMutator = Callable[[dict, random.Random, Ctx],
#: dict | None]`; `None` means 'not applicable to this plan'" -- the
#: convention `entity_drop` already uses (`faults_ext.py:186-190`).
PlanMutator = Callable[[dict[str, Any], random.Random, Ctx], "dict[str, Any] | None"]


# =========================================================================== #
# generic path-walk helpers, shared by surface-A and surface-B mutators       #
# =========================================================================== #

def _has_ref(x: Any) -> bool:
    """Mirrors `tgms.agent.verifier._has_ref`: true iff `x` is (or contains)
    a `{"$ref": ...}` binding -- such a subtree is not literally
    schema-checkable pre-execution."""
    if isinstance(x, dict):
        return set(x) == {"$ref"} or any(_has_ref(v) for v in x.values())
    if isinstance(x, list):
        return any(_has_ref(v) for v in x)
    return False


def _uid_leaf_paths(args: Any, path: tuple = (),
                    inside_uid_key: bool = False) -> list[tuple]:
    """Paths to literal strings sitting in uid-typed positions, structurally
    identical to `verifier._literal_uids` (which returns values, not paths)
    so the two cannot silently diverge on what counts as a uid position."""
    out: list[tuple] = []
    if isinstance(args, dict):
        if set(args) == {"$ref"}:
            return out
        for k, v in args.items():
            out += _uid_leaf_paths(v, path + (k,), inside_uid_key=k in UID_ARG_KEYS)
    elif isinstance(args, list):
        for i, v in enumerate(args):
            out += _uid_leaf_paths(v, path + (i,), inside_uid_key=inside_uid_key)
    elif inside_uid_key and isinstance(args, str):
        out.append(path)
    return out


def _get_path(obj: Any, path: tuple) -> Any:
    for k in path:
        obj = obj[k]
    return obj


def _set_path(obj: Any, path: tuple, value: Any) -> None:
    for k in path[:-1]:
        obj = obj[k]
    obj[path[-1]] = value


def _find_paths(obj: Any, pred: Callable[[Any, Any], bool],
               path: tuple = ()) -> list[tuple]:
    """Every path `p` such that `pred(key_at_p, value_at_p)` holds, walking
    dicts and lists. Used by the surface-B mutators, which work over a bare
    node dict with no uid-typed-key convention of its own."""
    out: list[tuple] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if pred(k, v):
                out.append(path + (k,))
            else:
                out += _find_paths(v, pred, path + (k,))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out += _find_paths(v, pred, path + (i,))
    return out


# =========================================================================== #
# §4 -- the four-way outcome classifier                                       #
# =========================================================================== #

class Outcome(str, Enum):
    CORRECT = "correct"
    SAFE_REFUSAL = "safe-refusal"
    EXPLICIT_FAILURE = "explicit-failure"
    SILENT_VIOLATION = "silent-violation"


#: §4's reason taxonomy for `safe-refusal`, **closed**: "Anything outside the
#: taxonomy is `explicit-failure`, never `safe-refusal`." `refresh-refused:*`
#: and `blocked-capability` are named here for completeness (and for E2's
#: F4 work, which shares this vocabulary) even though no F1/F2 cell in this
#: module currently produces them.
SAFE_REFUSAL_REASONS: tuple[str, ...] = (
    "cost-ceiling:plan", "cost-ceiling:node", "cost-ceiling:runtime",
    "incompleteness",
    "undecidable-freshness",
    "no-admissible-plan",
    "blocked-capability",
    "refresh-refused:not-found", "refresh-refused:generation-mismatch",
    "refresh-refused:no-refresh-handle", "refresh-refused:handle-mismatch",
    "refresh-refused:unknown-refresh-kind",
)

#: Sentinel meaning "the oracle does not supply this," distinct from a real
#: `None` gold value or a real `None` recompute value.
_NA = object()


@dataclass
class Oracle:
    """The independent-of-the-run-under-test facts `classify` compares
    against. Absent fields use `_NA` so "no oracle for this axis" (§2's
    un-oracled cells) is distinguishable from "the oracle says `None`."""

    gold: Any = _NA
    recompute_value: Any = _NA
    #: per-claim predicate, ground truth about which step's evidence a claim
    #: *should* have cited (§2's F1-9 reading: `misattributed`, never an I1
    #: violation, when the wrongly-cited payload happens to still support
    #: the value).
    cited_step_wrong: Callable[[dict[str, Any]], bool] | None = None


@dataclass
class Run:
    """Normalized inputs to `classify`, bundling what §4's pseudocode calls
    `run`/`trace`/`answer`/`report`/`certificate` into one object so the
    function stays pure and total over plain data -- testable with
    hand-built fixture dicts, no store, no trace, no LLM.

    `answer["claims"]` / `report["claims"]` are `[]` (or `answer` itself is
    `{}`) for a TGIR envelope: surface B has no AnswerObject, so the
    invariant loop below is a no-op and surface B correctly reduces to the
    refusal channel plus I4 -- §1's "Surface B probes I3/I4 and the refusal
    channel ... not claim support."

    `report["claims"][i]` carries, per claim, exactly what `classify` needs
    and nothing it has to re-derive from a trace: `verdict`, `reason`,
    `truncated` (bool), `cited_uids` (the grounding lexicon `ClaimVerifier`
    computed), `cited_values` (the numeric candidates it compared against).
    `augment_report` below builds this from a real `ClaimVerifier` report;
    fixtures build it by hand.
    """

    certificate: dict[str, Any] | None = None
    freshness: dict[str, Any] | None = None
    answer: dict[str, Any] | None = None
    answer_value: Any = None
    report: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


@dataclass
class Classification:
    outcome: Outcome
    reason: str | None = None
    invariants: list[tuple[str, str | None, str | None]] = field(default_factory=list)
    gold_mismatch: bool | None = None
    misattributed: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "reason": self.reason,
            "invariants_violated": [
                {"invariant": i, "claim_id": c, "detail": d}
                for i, c, d in self.invariants],
            "gold_mismatch": self.gold_mismatch,
            "misattributed": self.misattributed,
        }


def to_certificate(error: dict[str, Any] | None) -> dict[str, Any] | None:
    """Map an error payload onto §4's closed safe-refusal taxonomy, or
    `None` when it falls outside it.

    Surface B already builds a real object -- `RefusalCertificate.raise_`
    (`tgms/tgir/admission.py:100-110`) puts one under
    `details["refusal_certificate"]` with a `stage`; `IncompletenessRefusal
    .error` (`tgms/tgir/metadata.py:299-315`) puts one under
    `details["incompleteness_refusal"]`, `error_class="E_INCOMPLETE"`.

    Surface A has no certificate object at all -- its cost/limit/
    incompleteness refusals are plain `TgmsError.to_payload()` dicts, closed
    over exactly the codes/messages the design's §8 pre-registration and §4
    reason taxonomy name: the static cost pre-check (`verifier.py:180-187`,
    `E_COST`), the runtime wall-clock and row-count backstops
    (`executor.py:173`, `:248`, `E_LIMIT`), the reduce-over-truncated refusal
    (`executor.py:204-210`, `E_LIMIT`), and `harness.py`'s `E_NO_PLAN`. Any
    other code (`E_SCHEMA`, `E_GROUNDING`, `E_INVALID_ARG`, `E_NOT_FOUND`,
    `E_UPSTREAM`, a bare `E_LIMIT` step/hop cap, ...) is not in the closed
    taxonomy and returns `None` -- the caller then falls through to
    `explicit-failure`, exactly the F1 rows 1-5 reading.
    """
    if not error:
        return None
    details = error.get("details") or {}
    if "refusal_certificate" in details:
        cert = details["refusal_certificate"]
        return {"reason": f"cost-ceiling:{cert.get('stage', 'plan')}", "detail": cert}
    if "incompleteness_refusal" in details or details.get("error_class") == "E_INCOMPLETE":
        return {"reason": "incompleteness", "detail": details.get("incompleteness_refusal")}
    code = error.get("error")
    message = str(error.get("message") or "")
    if code == "E_COST":
        return {"reason": "cost-ceiling:node"}
    if code == "E_LIMIT":
        if "would reduce a truncated result" in message:
            return {"reason": "incompleteness"}
        if "plan wall clock" in message or "materialized rows" in message:
            return {"reason": "cost-ceiling:runtime"}
        return None
    if code == "E_NO_PLAN":
        return {"reason": "no-admissible-plan"}
    return None


def classify(run: Run, oracle: Oracle | None = None) -> Classification:
    """§4's pseudocode, verbatim in structure and order:

    1. refusal channel (certificate, then undecidable freshness) -- an
       answer's absence, justified.
    2. no answer at all -- `explicit-failure`, carrying the error code.
    3. an answer exists -> the four invariants, cited-evidence-relative.
    4. orthogonal flags (`gold_mismatch`, `misattributed`), always recorded,
       never part of the outcome branch above.

    Total: every `Run` reaches exactly one `return`. Pure: no I/O, no
    randomness, a function of `(run, oracle)` only.
    """
    oracle = oracle or Oracle()

    # 1. refusal channel
    if run.certificate is not None:
        return _finalize(
            Classification(Outcome.SAFE_REFUSAL, reason=run.certificate.get("reason")),
            run, oracle)
    if run.freshness is not None and run.freshness.get("kind") == "UNDECIDABLE":
        return _finalize(
            Classification(Outcome.SAFE_REFUSAL, reason="undecidable-freshness"),
            run, oracle)
    if run.answer is None:
        assert run.error is not None, \
            "classify: a run with no answer and no certificate must carry an error"
        return _finalize(
            Classification(Outcome.EXPLICIT_FAILURE, reason=(run.error or {}).get("error")),
            run, oracle)

    # 2. an answer exists -> the four invariants, cited-evidence-relative
    viol: list[tuple[str, str | None, str | None]] = []
    claims = run.answer.get("claims") or []
    reports = (run.report or {}).get("claims") or []
    for c, r in zip(claims, reports):
        truncated = bool(r.get("truncated"))
        if r.get("verdict") in ("unsupported", "unverifiable"):
            viol.append(("I1", c.get("id"), r.get("reason")))
        elif r.get("verdict") == "supported" and truncated:
            viol.append(("I1-trunc", c.get("id"), r.get("reason")))
        if c.get("type") == "entity":
            cited_uids = set(r.get("cited_uids") or [])
            if not set(c.get("uids") or []) <= cited_uids:
                viol.append(("I2", c.get("id"), "uid not in cited evidence"))
        if c.get("type") == "count" and isinstance(c.get("value"), (int, float)):
            cited_values = r.get("cited_values") or []
            want = float(c["value"])
            if not any(abs(float(v) - want) <= FLOAT_TOL for v in cited_values):
                viol.append(("I3", c.get("id"), "value not in cited evidence"))
    if run.freshness is not None and run.freshness.get("kind") == "FRESH" \
            and oracle.recompute_value is not _NA \
            and oracle.recompute_value != run.answer_value:
        viol.append(("I4", None, "FRESH contradicted by recomputation"))

    base = Classification(Outcome.SILENT_VIOLATION, invariants=viol) if viol \
        else Classification(Outcome.CORRECT)
    return _finalize(base, run, oracle)


def outcome_rates(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """§6: "add a separate `outcome_rates(rows)` that skips rows lacking
    `outcome`, so PVR/ESR/EM/F1/UCR/coverage keep their current meaning."
    Deliberately not placed in `tgms.eval.metrics` (out of this task's file
    ownership; `tgms/eval/metrics.py` is untouched) -- `rates()` there is
    unmodified and this is a separate, additive readout over the same rows.
    """
    have = [r for r in rows if "outcome" in r]
    n = len(have)
    if n == 0:
        return {"n": 0}
    counts = {o.value: 0 for o in Outcome}
    for r in have:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    gold_mismatch = sum(1 for r in have if r.get("gold_mismatch"))
    misattributed = sum(1 for r in have if r.get("misattributed"))
    return {"n": n, "counts": counts,
           "rates": {k: v / n for k, v in counts.items()},
           "gold_mismatch": gold_mismatch, "misattributed": misattributed}


def _finalize(base: Classification, run: Run, oracle: Oracle) -> Classification:
    """§4's orthogonal flags: `gold_mismatch = extract_pred(...) != oracle.gold`
    and `misattributed = any(cited_step_wrong(c) for c in claims)`. Computed
    once, attached to whichever branch `classify` took.

    **Named deviation from the pseudocode's literal reading**: §4 writes
    `gold_mismatch` unconditionally, but a run with no answer at all
    (`explicit-failure` / `safe-refusal`) has no `answer_value` to compare
    -- treating `None != oracle.gold` as "mismatched" would mark *every*
    failed/refused trial `gold_mismatch=True`, which is trivially true and
    swamps the one place §8's exact wording actually uses the flag: "k of n
    answers were evidence-consistent yet did not match gold," i.e. among
    answers that exist. So `gold_mismatch` is left `None` (not applicable)
    whenever `run.answer is None`, and computed only when an answer was
    actually produced.
    """
    if oracle.gold is not _NA and run.answer is not None:
        base.gold_mismatch = run.answer_value != oracle.gold
    if oracle.cited_step_wrong is not None and run.answer is not None:
        base.misattributed = any(oracle.cited_step_wrong(c)
                                 for c in (run.answer.get("claims") or []))
    return base


def gate_answer(answer_obj: dict[str, Any],
                report: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], int]:
    """The delivered-answer reading for this module's own classification
    (§2's F1 table names several cells' expected class "explicit-failure
    (gated)", describing a *gated* read, not an ungated one): drop a claim
    before classification whenever `classify`'s own I1 branch would flag
    it, i.e. `verdict in ("unsupported", "unverifiable")`.

    **Named discrepancy with `tgms/eval/harness.py`, deliberately not
    fixed here.** `run_task_ours`'s own gate (`harness.py:124-126`) is
    narrower: `kept = [c for c, r in ... if r["verdict"] != "unsupported"]`
    -- it does not drop `unverifiable` claims, so one reaches a real user
    of the deployed "ours" system today. This function uses the wider,
    classifier-parity rule instead, because (a) §4's `classify` itself
    treats the two verdicts identically for I1, and (b) §2's own F1-9 row
    cites "missing -> unverifiable" as *the* detection mechanism behind its
    "explicit-failure" expected class, which only holds if the delivered
    answer drops that claim too. Changing `harness.py`'s gate to match is
    out of this task's scope (E1 owns only its additive `outcome` field,
    "no metric semantics change") -- flagged here, and in the E1 report,
    as a real gap for a follow-up decision, not silently resolved.

    Returns `(gated_answer, gated_report, n_dropped)`; `gated_report
    ["claims"]` stays aligned with `gated_answer["claims"]`
    position-for-position, which is what `classify`'s `zip` needs.
    """
    claims = answer_obj.get("claims") or []
    reports = (report or {}).get("claims") or []
    kept_c, kept_r = [], []
    for c, r in zip(claims, reports):
        if r.get("verdict") in ("unsupported", "unverifiable"):
            continue
        kept_c.append(c)
        kept_r.append(r)
    return ({**answer_obj, "claims": kept_c}, {"claims": kept_r},
           len(claims) - len(kept_c))


def freshness_from_verdict(verdict: Any) -> dict[str, Any]:
    """Normalize a `tgms.tgir.check.Verdict` (`.state` in `{"fresh",
    "possibly-stale", "undecidable"}`) into the `{"kind": ...}` shape
    `classify` expects (`"FRESH" | "POSSIBLY_STALE" | "UNDECIDABLE"`)."""
    return {"kind": verdict.state.upper().replace("-", "_")}


def make_misattribution_oracle(
        original_claims: list[dict[str, Any]]) -> Callable[[dict[str, Any]], bool]:
    """§2's F1-9 reading, generically: a claim is misattributed iff its
    current evidence citation differs from the step it was originally,
    correctly, derived from -- regardless of whether the verdict still
    comes back `supported` (the undetected 64%, per `docs/site_facts.json`,
    where the value happens to also appear in the wrongly-cited payload)."""
    original = {c["id"]: tuple(c.get("evidence") or []) for c in original_claims}

    def _wrong(claim: dict[str, Any]) -> bool:
        orig = original.get(claim.get("id"))
        return orig is not None and tuple(claim.get("evidence") or []) != orig

    return _wrong


def augment_report(answer_obj: dict[str, Any], report: dict[str, Any],
                   verifier: Any) -> dict[str, Any]:
    """Bridge a real `ClaimVerifier` report into the shape `Run.report`
    needs: per claim, `truncated` / `cited_uids` / `cited_values` alongside
    the verifier's own `verdict` / `reason`."""
    from tgms.agent.verifier import _collect_all_strings, _collect_numbers

    claims = answer_obj.get("claims") or []
    out_claims = []
    for c, r in zip(claims, report.get("claims") or []):
        payloads, _missing, truncated = verifier._evidence_payloads(c.get("evidence") or [])
        uids: set[str] = set()
        nums: set[float] = set()
        for p in payloads:
            _collect_all_strings(p, uids)
            _collect_numbers(p, nums)
        out_claims.append({**r, "truncated": truncated,
                           "cited_uids": sorted(uids),
                           "cited_values": sorted(nums)})
    return {"claims": out_claims}


# =========================================================================== #
# cell registry -- §2 (F1) and §3 (F2), transcribed as data                   #
# =========================================================================== #

@dataclass(frozen=True)
class Cell:
    """One row of the memo's F1 or F2 table. `expected_outcome_text` is the
    table's own "Expected class" column, kept verbatim (including compound
    readings like "correct + gold-mismatch" or "explicit-failure, or
    correct if a scan fallback reproduces O-ENV") so a diff against the
    frozen memo is exact; `expected_outcome` is the single unambiguous
    `Outcome` for cells where the table names one, else `None`."""

    cell_id: str
    family: str                 # "F1" | "F2"
    surface: str                 # "A" | "B" | "A/B" | "A (trace)"
    fault: str
    mutation: str
    oracle: str
    expected_outcome_text: str
    expected_outcome: Outcome | None
    invariant: str | None
    headline: bool = True        # False: excluded from the headline count (§8)
    note: str = ""


CELL_REGISTRY: dict[str, Cell] = {
    c.cell_id: c for c in [
        Cell("F1-1", "F1", "A/B", "fabricated entity id",
             "rewrite a literal uid in Step.args (A, pre-validate) or a "
             "leaf arg (B)",
             "O-STATIC grounding rule verifier.py:169-175 (E_GROUNDING); "
             "else O-ENV",
             "explicit-failure", Outcome.EXPLICIT_FAILURE, "I2",
             note="Surface B has no grounding rule; §8 pre-registers it as "
                  "an open cell (outcome unknown, not asserted)."),
        Cell("F1-2", "F1", "A", "nonexistent property",
             "rewrite an arg name or a $ref field token (A, pre-validate)",
             "O-STATIC verifier.py:133-141 (unknown arg), :196-204 "
             "(answer_spec.from)",
             "explicit-failure", Outcome.EXPLICIT_FAILURE, "I1/I3"),
        Cell("F1-3a", "F1", "A/B", "invalid interval",
             "window.t_b <= t_a, or outside dataset extent (A/B)",
             "O-STATIC verifier.py:145-155 (E_INVALID_ARG)",
             "explicit-failure", Outcome.EXPLICIT_FAILURE, "I3"),
        Cell("F1-3b", "F1", "A/B", "valid but wrong interval",
             "shift t_a,t_b by delta (A/B)", "O-ENV only",
             "correct + gold-mismatch", Outcome.CORRECT, "-",
             headline=False,
             note="Un-oracled for the silent channel (§2): executes "
                  "cleanly, claim is supported by its own trace; only "
                  "gold_mismatch fires."),
        Cell("F1-4", "F1", "A", "invalid primitive",
             "replace op with an unregistered name",
             "O-STATIC verifier.py:113-117 (E_NOT_FOUND)",
             "explicit-failure", Outcome.EXPLICIT_FAILURE, "I1"),
        Cell("F1-5", "F1", "A/B", "malformed plan",
             "drop a required key, duplicate a step id, introduce a cycle",
             "O-STATIC verifier.py:85-108 (E_SCHEMA)",
             "explicit-failure", Outcome.EXPLICIT_FAILURE, "I1"),
        Cell("F1-6", "F1", "A", "semantically wrong operator",
             "swap for another registered op with a compatible arg schema",
             "none (see below)",
             "correct + gold-mismatch", Outcome.CORRECT, "-",
             headline=False,
             note="Un-oracled (§2): still type-checks, executes cleanly, "
                  "claim supported by its own trace. Already declared "
                  "uncovered (paper/ecqr/tab-fault.tex:21-23, "
                  "EVIDENCE_MODEL.md:228). The oracled sub-case (a swap "
                  "that changes the answer output field, caught by "
                  "verifier.py:196-204) is run as the separate "
                  "'F1-6b' mutator per the memo's own instruction, not as "
                  "a 12th table row."),
        Cell("F1-7", "F1", "A (trace)", "arithmetic outside trusted operator",
             "reporter emits a claim whose value is arithmetic over two "
             "step outputs with no compute step (A)",
             "O-TRACE verifier.py:354-380",
             "explicit-failure (gated)", Outcome.EXPLICIT_FAILURE, "I3",
             note="Reused, not reimplemented: answer-level, "
                  "MUTATORS['count_pm1'] (faults.py:87-92)."),
        Cell("F1-8", "F1", "A/B", "unsupported aggregation",
             "aggregate over a limit-ed / non-complete input",
             "O-LATTICE; A-analogue executor.py:204-210",
             "safe-refusal", Outcome.SAFE_REFUSAL, "I1/I3"),
        Cell("F1-9", "F1", "A (trace)", "stale trace reference",
             "re-point a claim's evidence at a step of a different run",
             "O-TRACE verifier.py:338-350 (missing -> unverifiable)",
             "explicit-failure", Outcome.EXPLICIT_FAILURE, "I1",
             note="Reused: EXT_MUTATORS['wrong_step_citation'] "
                  "(faults_ext.py:200-206). §2's pre-registered reading: "
                  "the 36/100-detected cases are I1 violations; the "
                  "undetected 64/100 remain `correct` (still supported by "
                  "their own, wrongly-cited, evidence) with "
                  "`misattributed=True` -- never counted as I1 violations. "
                  "See `make_misattribution_oracle`."),
        Cell("F1-10", "F1", "A (trace)", "unsupported final claim",
             "fabricate a value / uid",
             "O-TRACE",
             "explicit-failure (gated)", Outcome.EXPLICIT_FAILURE, "I1/I2",
             note="Reused: MUTATORS['uid_swap'] / ['count_pm1'] "
                  "(faults.py:87-92)."),
        Cell("F1-11", "F1", "A (trace)", "claim over truncated output",
             "shrink a paginated step's limit",
             "O-REC full rows_total",
             "safe-refusal (weak support)", Outcome.SAFE_REFUSAL, "I1",
             note="Reused: truncated_count_cases (faults_ext.py:223)."),

        Cell("F2-1", "F2", "A", "operator exception",
             "router raises at step k", "O-ENV",
             "explicit-failure", Outcome.EXPLICIT_FAILURE, "I1"),
        Cell("F2-2", "F2", "A", "timeout",
             "wall-clock budget in the wrapper", "O-ENV",
             "explicit-failure", Outcome.EXPLICIT_FAILURE, "I1",
             note="No timeout mechanism exists today (only cost ceilings "
                  "and the runtime Budget, execute.py:59-68); the injector "
                  "synthesizes one (§3)."),
        Cell("F2-3", "F2", "A", "partial result (silent)",
             "return fewer rows without truncated=True", "none",
             "silent-violation by construction", Outcome.SILENT_VIOLATION,
             "-", headline=False,
             note="Outside the trust model: assumption A2 "
                  "(EVIDENCE_MODEL.md:236-238) says the backend truthfully "
                  "constructs the capabilities it emits. Reported as a "
                  "stated assumption exercised, not a headline detection."),
        Cell("F2-4", "F2", "A", "honest truncation",
             "low limit, truncated=True", "O-REC",
             "safe-refusal", Outcome.SAFE_REFUSAL, "I1"),
        Cell("F2-5", "F2", "A", "resource-limit violation",
             "force estimates over ceilings", "O-CERT",
             "safe-refusal", Outcome.SAFE_REFUSAL, "-"),
        Cell("F2-6", "F2", "A", "unavailable index",
             "adapter wrapper hides an index", "O-ENV",
             "explicit-failure, or correct if a scan fallback reproduces "
             "O-ENV", None, "I1",
             note="Disjunctive expected class per the table; "
                  "`expected_outcome` left unset (both alternatives are "
                  "pre-registered, not one)."),
        Cell("F2-7", "F2", "A/B", "stale index metadata", "-",
             "M4/M5 records", "by reference", None, "I4",
             headline=False,
             note="Reuses the M5 freshness campaign's populations "
                  "(benchmarks/m5-v1/*.json) by reference; not rerun here "
                  "(§3: 'F2-7 reuses the M5 populations; do not rerun')."),
    ]
}

F1_CELL_IDS: tuple[str, ...] = tuple(c for c in CELL_REGISTRY if c.startswith("F1-"))
F2_CELL_IDS: tuple[str, ...] = tuple(c for c in CELL_REGISTRY if c.startswith("F2-"))


# =========================================================================== #
# PLAN_MUTATORS -- surface A, over Plan.to_json()                             #
# =========================================================================== #

def f1_1_fabricated_entity_id(plan_json: dict[str, Any], rng: random.Random,
                              ctx: Ctx | None = None) -> dict[str, Any] | None:
    """Rewrite a literal uid sitting in a uid-typed arg position
    (`UID_ARG_KEYS`) to a fabricated id nothing in the store could ground."""
    out = copy.deepcopy(plan_json)
    cands = [(si, p) for si, s in enumerate(out.get("steps") or [])
             for p in _uid_leaf_paths(s.get("args") or {})]
    if not cands:
        return None
    si, p = cands[rng.randrange(len(cands))]
    _set_path(out["steps"][si]["args"], p, f"fabricated-{rng.randrange(1_000_000)}")
    return out


def f1_2_nonexistent_property(plan_json: dict[str, Any], rng: random.Random,
                              ctx: Ctx | None = None) -> dict[str, Any] | None:
    """Rename a top-level arg key so it no longer names anything the
    operator's schema declares (`E_SCHEMA: unknown arg`)."""
    out = copy.deepcopy(plan_json)
    cands = [si for si, s in enumerate(out.get("steps") or []) if s.get("args")]
    if not cands:
        return None
    si = cands[rng.randrange(len(cands))]
    args = out["steps"][si]["args"]
    key = sorted(args.keys())[rng.randrange(len(args))]
    args[f"{key}_bogus"] = args.pop(key)
    return out


def f1_3a_invalid_interval(plan_json: dict[str, Any], rng: random.Random,
                           ctx: Ctx | None = None) -> dict[str, Any] | None:
    """Force `window.t_a >= window.t_b` on some step -- always genuinely
    invalid regardless of the original ordering."""
    out = copy.deepcopy(plan_json)
    cands = [si for si, s in enumerate(out.get("steps") or [])
             if isinstance((s.get("args") or {}).get("window"), dict)
             and {"t_a", "t_b"} <= set(s["args"]["window"])
             and isinstance(s["args"]["window"]["t_a"], int)
             and isinstance(s["args"]["window"]["t_b"], int)]
    if not cands:
        return None
    si = cands[rng.randrange(len(cands))]
    w = out["steps"][si]["args"]["window"]
    a, b = w["t_a"], w["t_b"]
    w["t_a"], w["t_b"] = max(a, b) + 1, min(a, b)
    return out


def f1_3b_valid_but_wrong_interval(plan_json: dict[str, Any], rng: random.Random,
                                   ctx: Ctx | None = None) -> dict[str, Any] | None:
    """Shift a window by a whole span, staying inside the dataset extent
    when `ctx["stats"]` is supplied -- valid, but a different interval."""
    ctx = ctx or {}
    stats = ctx.get("stats") or {}
    vt_min, vt_max = stats.get("vt_min"), stats.get("vt_max")
    out = copy.deepcopy(plan_json)
    cands = [si for si, s in enumerate(out.get("steps") or [])
             if isinstance((s.get("args") or {}).get("window"), dict)
             and {"t_a", "t_b"} <= set(s["args"]["window"])
             and isinstance(s["args"]["window"]["t_a"], int)
             and isinstance(s["args"]["window"]["t_b"], int)]
    if not cands:
        return None
    si = cands[rng.randrange(len(cands))]
    w = out["steps"][si]["args"]["window"]
    span = max(1, w["t_b"] - w["t_a"])
    for direction in (rng.choice([-1, 1]), 1, -1):
        na, nb = w["t_a"] + span * direction, w["t_b"] + span * direction
        if na >= nb:
            continue
        if vt_min is not None and (na < vt_min or nb > vt_max):
            continue
        w["t_a"], w["t_b"] = na, nb
        return out
    return None


def f1_4_invalid_primitive(plan_json: dict[str, Any], rng: random.Random,
                           ctx: Ctx | None = None) -> dict[str, Any] | None:
    """Replace a step's `op` with a name no operator registers."""
    out = copy.deepcopy(plan_json)
    steps = out.get("steps") or []
    if not steps:
        return None
    si = rng.randrange(len(steps))
    steps[si]["op"] = f"nonexistent_operator_{rng.randrange(10_000)}"
    return out


def f1_5_malformed_plan(plan_json: dict[str, Any], rng: random.Random,
                        ctx: Ctx | None = None) -> dict[str, Any] | None:
    """One of: drop a step's required `op` key, duplicate a step id, or
    introduce a two-step dependency cycle -- chosen by `rng` among whichever
    are applicable to this plan's step count."""
    out = copy.deepcopy(plan_json)
    steps = out.get("steps") or []
    variants = []
    if steps:
        variants.append("drop_op")
    if len(steps) >= 2:
        variants += ["dup_id", "cycle"]
    if not variants:
        return None
    v = variants[rng.randrange(len(variants))]
    if v == "drop_op":
        si = rng.randrange(len(steps))
        del steps[si]["op"]
    elif v == "dup_id":
        i, j = rng.sample(range(len(steps)), 2)
        steps[j]["id"] = steps[i]["id"]
    else:  # cycle
        i, j = rng.sample(range(len(steps)), 2)
        steps[i]["depends_on"] = sorted(set(steps[i].get("depends_on", [])
                                            + [steps[j]["id"]]))
        steps[j]["depends_on"] = sorted(set(steps[j].get("depends_on", [])
                                            + [steps[i]["id"]]))
    return out


def f1_6_semantic_operator_swap(plan_json: dict[str, Any], rng: random.Random,
                                ctx: Ctx | None = None) -> dict[str, Any] | None:
    """Swap a step's op for another *registered* op whose args_schema the
    step's own (literal, ref-free) args already validate against, and whose
    output contract is unchanged -- "still type-checks, executes cleanly,"
    the un-oracled reading (§2's F1-6)."""
    from tgms.temporal.algebra import REGISTRY, ensure_all_registered

    ensure_all_registered()
    out = copy.deepcopy(plan_json)
    steps = out.get("steps") or []
    order = list(range(len(steps)))
    rng.shuffle(order)
    for si in order:
        step = steps[si]
        if _has_ref(step.get("args") or {}):
            continue
        cur_spec = REGISTRY.get(step["op"])
        if cur_spec is None:
            continue
        candidates = [n for n in REGISTRY if n != step["op"]]
        rng.shuffle(candidates)
        for cand in candidates:
            cspec = REGISTRY[cand]
            if cspec.output_fields != cur_spec.output_fields:
                continue
            try:
                jsonschema.validate(step["args"], cspec.args_schema)
            except jsonschema.ValidationError:
                continue
            steps[si]["op"] = cand
            return out
    return None


def f1_6b_operator_swap_breaks_output_contract(
        plan_json: dict[str, Any], rng: random.Random,
        ctx: Ctx | None = None) -> dict[str, Any] | None:
    """The one oracled sub-case of F1-6 (§2): a swap of the step the answer
    reads from, into an op whose output no longer carries the field
    `answer_spec.from` names -- caught statically by
    `verifier._check_output_field` (`verifier.py:196-204`). Registered as
    its own mutator per the memo's instruction to "run it as a separate
    cell," not as a 12th row of the frozen F1 table."""
    from tgms.temporal.algebra import REGISTRY, ensure_all_registered

    ensure_all_registered()
    out = copy.deepcopy(plan_json)
    steps = out.get("steps") or []
    root = (out.get("answer_spec") or {}).get("from", "")
    if "." not in root:
        return None
    field_name = root.split(".")[1].split("[")[0]
    order = list(range(len(steps)))
    rng.shuffle(order)
    for si in order:
        step = steps[si]
        if not (root == step["id"] or root.startswith(step["id"] + ".")):
            continue
        if _has_ref(step.get("args") or {}):
            continue
        cur_spec = REGISTRY.get(step["op"])
        if cur_spec is None or field_name not in cur_spec.output_fields:
            continue
        candidates = [n for n in REGISTRY if n != step["op"]]
        rng.shuffle(candidates)
        for cand in candidates:
            cspec = REGISTRY[cand]
            if field_name in cspec.output_fields:
                continue
            try:
                jsonschema.validate(step["args"], cspec.args_schema)
            except jsonschema.ValidationError:
                continue
            steps[si]["op"] = cand
            return out
    return None


PLAN_MUTATORS: dict[str, PlanMutator] = {
    "F1-1": f1_1_fabricated_entity_id,
    "F1-2": f1_2_nonexistent_property,
    "F1-3a": f1_3a_invalid_interval,
    "F1-3b": f1_3b_valid_but_wrong_interval,
    "F1-4": f1_4_invalid_primitive,
    "F1-5": f1_5_malformed_plan,
    "F1-6": f1_6_semantic_operator_swap,
    "F1-6b": f1_6b_operator_swap_breaks_output_contract,
}


# =========================================================================== #
# TGIR_MUTATORS -- surface B, over the `root` node dict                       #
# =========================================================================== #

def tg_f1_1_fabricated_uid(root_json: Any, rng: random.Random,
                           ctx: Ctx | None = None) -> Any | None:
    """Rewrite one entry of a literal `uids` list anywhere in the node tree
    (`NodeScan.uids`, `Endpoints.uids`) to a fabricated id."""
    if not isinstance(root_json, (dict, list)):
        return None
    out = copy.deepcopy(root_json)
    paths = _find_paths(
        out, lambda k, v: k == "uids" and isinstance(v, list) and v
        and all(isinstance(x, str) for x in v))
    if not paths:
        return None
    p = paths[rng.randrange(len(paths))]
    lst = _get_path(out, p)
    idx = rng.randrange(len(lst))
    lst[idx] = f"fabricated-{rng.randrange(1_000_000)}"
    return out


def tg_f1_3a_invalid_interval(root_json: Any, rng: random.Random,
                              ctx: Ctx | None = None) -> Any | None:
    """Stamp an invalid (`t_a > t_b`) `sigma` override directly on the root
    node -- `loader.py`'s `_sigma`/`Interval` construction rejects it before
    the node can be built, the surface-B analogue of a static rejection."""
    if not isinstance(root_json, dict):
        return None
    out = copy.deepcopy(root_json)
    ctx = ctx or {}
    t_b_field = (ctx.get("sigma") or {}).get("t_b", 4_611_686_018_427_387_904)
    out["sigma"] = {"t_v": [[10, 5]], "t_b": t_b_field}
    return out


def tg_f1_3b_valid_but_wrong_interval(root_json: Any, rng: random.Random,
                                      ctx: Ctx | None = None) -> Any | None:
    """Stamp a validly-ordered but shifted `sigma` override on the root
    node -- executes cleanly against a different window."""
    if not isinstance(root_json, dict):
        return None
    out = copy.deepcopy(root_json)
    ctx = ctx or {}
    base = ctx.get("sigma") or {}
    t_v = base.get("t_v") or [[0, 1000]]
    t_a, t_b = int(t_v[0][0]), int(t_v[0][1])
    span = max(1, t_b - t_a)
    shift = span * rng.choice([-2, 2])
    out["sigma"] = {"t_v": [[max(0, t_a + shift), t_b + shift]],
                    "t_b": base.get("t_b", 4_611_686_018_427_387_904)}
    return out


def tg_f1_5_malformed(root_json: Any, rng: random.Random,
                      ctx: Ctx | None = None) -> Any | None:
    """Drop the root node's `op` key -- `loader.py`'s `_node` cannot resolve
    a node type, raising before anything runs."""
    if not isinstance(root_json, dict) or "op" not in root_json:
        return None
    out = copy.deepcopy(root_json)
    del out["op"]
    return out


TgirMutator = Callable[[Any, random.Random, Ctx], "Any | None"]

TGIR_MUTATORS: dict[str, TgirMutator] = {
    "F1-1": tg_f1_1_fabricated_uid,
    "F1-3a": tg_f1_3a_invalid_interval,
    "F1-3b": tg_f1_3b_valid_but_wrong_interval,
    "F1-5": tg_f1_5_malformed,
}


# =========================================================================== #
# EXEC_FAULTS -- F2 wrappers around ToolRouter / the storage adapter          #
# =========================================================================== #

class FaultingRouter:
    """Wraps a `ToolRouter`-shaped object (anything with `.call(name, args)`
    and `.tools()`) to inject exactly one F2 execution fault at the
    `target_call`-th `.call()` (1-based), deterministic from `rng`. Never
    edits the plan (§3: "Injected by wrapping ToolRouter ... never by
    editing plans")."""

    def __init__(self, inner: Any, fault: str, target_call: int,
                rng: random.Random, wall_budget_s: float = 0.05) -> None:
        self.inner = inner
        self.fault = fault
        self.target_call = target_call
        self.rng = rng
        self.wall_budget_s = wall_budget_s
        self.n_calls = 0
        self.injected = False
        self.true_rows_total: int | None = None
        self.adapter = getattr(inner, "adapter", None)

    def tools(self) -> list[str]:
        return self.inner.tools()

    def leaf_meta(self, *a: Any, **k: Any) -> dict[str, Any]:
        return self.inner.leaf_meta(*a, **k) if hasattr(self.inner, "leaf_meta") else {}

    def read_basis(self, *a: Any, **k: Any) -> dict[str, Any]:
        return self.inner.read_basis(*a, **k) if hasattr(self.inner, "read_basis") else {}

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.n_calls += 1
        if self.n_calls != self.target_call:
            return self.inner.call(name, args)
        self.injected = True

        if self.fault == "operator_exception":
            # F2-1: a raised exception, turned into the router's ordinary
            # error-payload contract (`tools/server.py:40-50` never lets a
            # TgmsError escape `.call`) -- indistinguishable downstream from
            # any other operator failure, which is the point.
            try:
                raise InternalError(f"injected operator exception at step "
                                    f"{self.target_call} (F2-1)")
            except TgmsError as e:
                return e.to_payload()

        if self.fault == "timeout":
            # F2-2: "the injector must synthesize" a timeout -- no
            # wall-clock mechanism exists today (§3).
            time.sleep(self.wall_budget_s)
            return {"error": "E_LIMIT",
                    "message": f"operator {name} exceeded the injected "
                                f"{self.wall_budget_s:.3f}s wall-clock "
                                "budget (F2-2)",
                    "details": {}}

        res = self.inner.call(name, args)
        if "error" in res or not isinstance(res.get("rows"), list) \
                or len(res["rows"]) < 2:
            return res  # nothing to shrink -- report the real result as-is

        if self.fault == "silent_partial":
            # F2-3: fewer rows, `truncated` still False -- the assumption
            # A2 violation, undetectable at the evidence layer by design.
            self.true_rows_total = int(res.get("rows_total", len(res["rows"])))
            out = dict(res)
            out["rows"] = res["rows"][:-1]
            out["rows_total"] = len(out["rows"])
            out["truncated"] = False
            return out

        if self.fault == "honest_truncation":
            # F2-4: fewer rows, honestly disclosed.
            self.true_rows_total = int(res.get("rows_total", len(res["rows"])))
            out = dict(res)
            out["rows"] = res["rows"][:-1]
            out["truncated"] = True
            out["rows_total"] = self.true_rows_total
            return out

        if self.fault == "unavailable_index":
            try:
                raise NotFoundError(f"injected index unavailability for "
                                    f"{name} (F2-6)")
            except TgmsError as e:
                return e.to_payload()

        return res


ExecFaultFactory = Callable[[Any, int, random.Random], FaultingRouter]

EXEC_FAULTS: dict[str, ExecFaultFactory] = {
    "F2-1": lambda inner, target, rng: FaultingRouter(inner, "operator_exception", target, rng),
    "F2-2": lambda inner, target, rng: FaultingRouter(inner, "timeout", target, rng),
    "F2-3": lambda inner, target, rng: FaultingRouter(inner, "silent_partial", target, rng),
    "F2-4": lambda inner, target, rng: FaultingRouter(inner, "honest_truncation", target, rng),
    "F2-6": lambda inner, target, rng: FaultingRouter(inner, "unavailable_index", target, rng),
}


def tiny_cost_ceilings() -> dict[str, int]:
    """F2-5's injection: ceilings so low the very first operator call trips
    `E_COST` inside the existing `enforce_cost` path (`algebra.py`) -- no
    new admission code needed, only a hostile ceilings dict passed to
    `ToolRouter(adapter, cost_ceilings=tiny_cost_ceilings())`."""
    from tgms.temporal.guardrails import DEFAULT_CEILINGS

    return {k: 0 for k in DEFAULT_CEILINGS}


def f2_7_stale_index_metadata_by_reference() -> None:
    """F2-7 is **not implemented here** and must never be executed by this
    module's driver: §3 reuses the M5 freshness campaign's own populations
    (`benchmarks/m5-v1/*.json`) "by reference; do not rerun." Calling this
    is a bug in the driver, not a missing feature."""
    raise NotImplementedError(
        "F2-7 (stale index metadata) is reported by reference to the M5 "
        "freshness campaign (benchmarks/m5-v1/*.json), per the frozen "
        "design memo §3 ('F2-7 reuses the M5 populations; do not rerun'). "
        "It is deliberately not re-executed by tgms.eval.plan_faults.")


__all__ = [
    "CELL_REGISTRY", "F1_CELL_IDS", "F2_CELL_IDS", "Cell",
    "Classification", "EXEC_FAULTS", "FaultingRouter", "Oracle",
    "Outcome", "PLAN_MUTATORS", "PlanMutator", "Run", "SAFE_REFUSAL_REASONS",
    "TGIR_MUTATORS", "TgirMutator", "augment_report", "classify", "gate_answer",
    "outcome_rates",
    "f2_7_stale_index_metadata_by_reference", "freshness_from_verdict",
    "make_misattribution_oracle", "tiny_cost_ceilings", "to_certificate",
    # reused, re-exported (§6)
    "MUTATORS", "EXT_MUTATORS", "c2_readout_from_suite", "run_fault_injection",
    "truncated_count_cases", "wrong_belief_state_cases",
]
