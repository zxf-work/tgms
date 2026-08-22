"""Fixtures for the M4.3 soundness suite — stores, scopes, and verdict readers.

This module is written from the frozen documents alone
(`docs/design/FRESHNESS_SEMANTICS.md` §§1–14 **and its append-only errata
register §15**, `docs/design/tgir_b1/B2C_GATE_REVIEW.md`,
`docs/design/M4_IMPLEMENTATION_PLAN.md` §M4.2/§M4.3) and never from
`tgms/tgir/footprint.py` or `tgms/tgir/check.py`, which are written in parallel.
It is the suite's *only* coupling point to the production surface, so a
divergence between the declared interface and the implementation shows up here
rather than being absorbed into twenty-five assertions.

Two disciplines it exists to enforce:

- **Real writes through the real store API.** Every scenario is built by calling
  `Store.assert_node` / `assert_edge` / `correct` / `retract` / `ingest_events`,
  so the event log the checker reads is the log the applier wrote. A footprint
  that disagrees with `apply_ops` is exactly the false-freshness source
  M4's §3.2 obligation 1 names, and a suite that hand-rolled log records could
  not see it.
- **Ground truth by recompute-and-compare (D1.11, D6.1).** Every "the result
  changed" claim in the suite is a recomputation with the cost guardrail
  bypassed (`skip_cost_check=True`, §1.6), never an assertion about the store.
"""

from __future__ import annotations

import os
from dataclasses import replace
from typing import Any

import tgms
from tgms.core.model import OPEN_END
from tgms.temporal.algebra import call_operator, ensure_all_registered
from tgms.tgir.depscope import TOP, DependencyScope, ScopeTerm
from tgms.tgir.scope_of import ScopeBasis, leaf_scope, scope_of
from tgms.tgir.ttq import basis_of

#: The suite runs on whichever backend the rest of `tests/` runs on. Both are
#: legitimate: a cursorless backend (DuckDB) carries `FULL_SCAN_CHECKPOINTS`,
#: which is D13.8a's sanctioned widening fallback — "slow, never wrong" — and
#: the native engine carries real `(offset, chain)` pairs. Nothing in this
#: suite's claims turns on which, and both must pass.
BACKEND = os.environ.get("TGMS_TEST_BACKEND", "duckdb")


# ---------------------------------------------------------------------------
# stores
# ---------------------------------------------------------------------------

def open_store(tmp_path, name: str = "s"):
    """A real `Store` on disk — the only kind a freshness check can run against.

    An adapter-only handle carries `store = "unanchored"` (M4 plan §3.0), which
    D13.24 step 3 turns into `UNDECIDABLE("store-mismatch")` for every scope. So
    the whole suite needs a store directory with a real event log behind it.
    """
    ensure_all_registered()
    return tgms.open(tmp_path / name, backend=BACKEND)


def build_s0(store) -> None:
    """The toy store `S₀` of FRESHNESS_SEMANTICS §3.0, one batch per line, in
    order — `tt₁ < tt₂ < tt₃`, and `tt_q = tt₃`.

    ```
    tt₁  ingest_events([A→B @10, A→C @20, B→D @30])
    tt₂  assert_node("X", "Node", {}, vt_s=0, vt_e=OPEN_END)
    tt₃  assert_edge("A", "X", "ROLE", {"level": 1}, vt_s=0, vt_e=100)
    ```
    """
    store.ingest_events([
        {"src": "A", "dst": "B", "rel_type": "MSG", "vt_s": 10},   # ev1
        {"src": "A", "dst": "C", "rel_type": "MSG", "vt_s": 20},   # ev2
        {"src": "B", "dst": "D", "rel_type": "MSG", "vt_s": 30},   # ev3
    ])
    store.assert_node("X", "Node", {}, 0, OPEN_END)
    store.assert_edge("A", "X", "ROLE", {"level": 1}, 0, 100)


# ---------------------------------------------------------------------------
# results and scopes
# ---------------------------------------------------------------------------

def call(store, op: str, args: dict[str, Any]) -> dict[str, Any]:
    """One operator call against the store, with the guardrail bypassed.

    D6.1 step 3: the ground-truth recomputation runs with `skip_cost_check=True`
    so that an admission refusal caused by the store having *grown* is recorded
    as its own outcome class (§1.6's `REFUSED_ON_RECOMPUTE`) instead of
    contaminating the changed/unchanged comparison.
    """
    return call_operator(store.adapter, op, dict(args), skip_cost_check=True,
                         tt_source=store)


def digest_of(store, op: str, args: dict[str, Any]) -> str:
    """`result_digest` — D1.8's canonical payload digest, the *only* admissible
    definition of "the same result"."""
    return call(store, op, args)["result_digest"]


def scope_of_call(store, op: str, args: dict[str, Any]) -> DependencyScope:
    """The `DependencyScope` the live envelope carries for this call (D13.16's
    placement), parsed back off the wire."""
    return DependencyScope.from_json(call(store, op, args)["dependency"])


def basis_for(store) -> ScopeBasis:
    """The read basis — store identity, clamped `tt_q`, checkpoints — captured
    **before** a read, per D13.17."""
    return basis_of(store.adapter, OPEN_END, store)


def scope_of_node(store, node) -> DependencyScope:
    """`scope_of(node) = leaf_scope(node) ⊎ ⊎ ins` — the whole plan's scope,
    every node's contributing (D13.14 prohibition 1)."""
    return scope_of(node, basis_for(store))


def leaf_scope_of(store, node) -> DependencyScope:
    """One node's *own* leaf scope, with no input scopes unioned in.

    Used where a scenario's claim is about one operator's term — L13.2a's
    `nodes` arm, RG-10's dropped `𝒟` arm — and the union with an upstream
    scope would catch the correction for a different reason and hide it.
    """
    return leaf_scope(node, basis_for(store))


def scope_from_terms(store, *terms: ScopeTerm) -> DependencyScope:
    """A scope carrying terms this suite wrote down from §9 directly.

    Two of the twenty-one scenarios name a per-operator domain that the M4 tree
    does not yet derive (`version_history`'s `V = window`, §9.6; `co_active`'s
    wired window, §9.10 — both carry the coarse `"*"` fallback today, which is
    a widening and therefore sound but cannot exhibit the conjunct the contract
    says catches them). For those, the suite hands `check` the scope **§9
    specifies** so the named arm is the one under test. D13.1 makes the live
    fallback a superset of it, which the same tests assert separately.
    """
    return basis_for(store).scope(*terms)


def widened(scope: DependencyScope, component: str) -> DependencyScope:
    """`scope` with one component of every term replaced by ⊤ (D4.5, D13.1).

    Widening is always sound, so a widened scope must never become `FRESH`
    where the narrow one was `POSSIBLY_STALE`.
    """
    return scope.with_terms(replace(t, **{component: TOP}) for t in scope.terms)


# ---------------------------------------------------------------------------
# the checker, and the verdict readers
# ---------------------------------------------------------------------------

def check_scope(store, scope: DependencyScope, **kwargs):
    """`check(scope, log, tt_now=OPEN_END)` — D13.24, per the M4 plan §3.4.

    `tt_now` is deliberately left at its default here: §9.1's recommendation is
    `OPEN_END` (scan the whole suffix), because `tt_now` rounds **up** where
    `tt_q` rounds down, and the log leads the applied frontier.
    """
    from tgms.tgir.check import check           # imported late: M4.2's module

    return check(scope, store.eventlog, **kwargs)


def check_record(store, record: dict[str, Any], **kwargs):
    """`check_trace(record, log, tt_now=OPEN_END)` — the plan/trace surface
    (M4 plan §3.6b), which is where per-step attribution lives (D5.4)."""
    from tgms.tgir.check import check_trace

    return check_trace(record, store.eventlog, **kwargs)


_VERDICT_NAMES = ("FRESH", "POSSIBLY_STALE", "UNDECIDABLE")


def _tag(raw: Any) -> str:
    if isinstance(raw, str):
        name = raw.replace("-", "_").replace(" ", "_").upper()
        if name in _VERDICT_NAMES:
            return name
    return ""


def verdict_name(verdict: Any) -> str:
    """The verdict's name, read defensively across the shapes D13.24's grammar
    (`FRESH | POSSIBLY_STALE(witnesses) | UNDECIDABLE(reason)`) admits in
    Python. The plan fixes the *grammar* and `.actionable_fresh`, not the
    attribute that spells the tag, so the tag is looked for and the structural
    reading is the fallback rather than the rule."""
    for attr in ("state", "verdict", "status", "kind", "name", "tag", "value"):
        raw = getattr(verdict, attr, None)
        if _tag(raw):
            return _tag(raw)
        if _tag(getattr(raw, "name", None)):
            return _tag(getattr(raw, "name", None))
        if _tag(getattr(raw, "value", None)):
            return _tag(getattr(raw, "value", None))
    if getattr(verdict, "actionable_fresh", False):
        return "FRESH"
    if reason_of(verdict):
        return "UNDECIDABLE"
    if witnesses(verdict):
        return "POSSIBLY_STALE"
    text = str(verdict).upper()
    for name in _VERDICT_NAMES:
        if name in text:
            return name
    return "FRESH"


def is_fresh(verdict: Any) -> bool:
    """D13.25: `UNDECIDABLE` is **not** a third contract — every consumer treats
    it as `POSSIBLY_STALE`, and the `Verdict` type says so by exposing
    `.actionable_fresh` rather than an enum comparison (M4 plan §3.7)."""
    flag = getattr(verdict, "actionable_fresh", None)
    if isinstance(flag, bool):
        return flag
    return verdict_name(verdict) == "FRESH"


def reason_of(verdict: Any) -> str:
    raw = getattr(verdict, "reason", None) or getattr(verdict, "reasons", None)
    if raw is None:
        return ""
    if isinstance(raw, (list, tuple, set)):
        return " ".join(str(r) for r in raw)
    return str(raw)


_WITNESS_FIELDS = ("batch_id", "tt", "op_seq", "arm", "class", "cls", "kind",
                   "identity", "vt", "matched_term", "matched_on", "step_id")


def _as_witness(w: Any) -> dict[str, Any]:
    """D13.27's object, however the implementation carries it."""
    if isinstance(w, dict):
        out = dict(w)
    else:
        to_json = getattr(w, "to_json", None)
        if callable(to_json):
            out = dict(to_json())
        else:
            out = {f: getattr(w, f) for f in _WITNESS_FIELDS if hasattr(w, f)}
    if "class" not in out and "cls" in out:
        out["class"] = out["cls"]
    out.setdefault("matched_on", [])
    return out


def witnesses(verdict: Any) -> list[dict[str, Any]]:
    """Every witness a verdict carries, flattened across the per-step map a
    `PlanVerdict` reports alongside its one bit (D5.4, §13.8.4)."""
    raw = getattr(verdict, "witnesses", None)
    if raw:
        return [_as_witness(w) for w in raw]
    out: list[dict[str, Any]] = []
    for attr in ("steps", "per_step", "by_step"):
        holder = getattr(verdict, attr, None)
        if not holder:
            continue
        values = holder.values() if hasattr(holder, "values") else holder
        for sub in values:
            out.extend(witnesses(sub))
        if out:
            return out
    return []


def arms(verdict: Any) -> set[str]:
    """Which footprint arms fired — `"value"`, `"carve"`, or both (D13.21a).

    This is the field several scenarios turn on: FF-1 and RG-1 are caught by
    the **carve** arm alone, and CE-5's whole point is that the **value** arm is
    enough, which is what lets `aggregate_events` keep its window.
    """
    return {w.get("arm") for w in witnesses(verdict) if w.get("arm")}


def kinds(verdict: Any) -> set[str]:
    return {w.get("kind") for w in witnesses(verdict) if w.get("kind")}


def steps_hit(verdict: Any) -> set[str]:
    return {w.get("step_id") for w in witnesses(verdict) if w.get("step_id")}


def matched_on(verdict: Any) -> set[str]:
    out: set[str] = set()
    for w in witnesses(verdict):
        out.update(w.get("matched_on") or ())
    return out


def describe(verdict: Any) -> str:
    """A failure message that names what actually fired, so a red test says
    *which* conjunct went missing rather than only that one did."""
    return (f"{verdict_name(verdict)}"
            f"{' ' + reason_of(verdict) if reason_of(verdict) else ''}"
            f" arms={sorted(arms(verdict))} kinds={sorted(kinds(verdict))}"
            f" matched_on={sorted(matched_on(verdict))}"
            f" steps={sorted(steps_hit(verdict))}")


# ---------------------------------------------------------------------------
# assertions the suite repeats
# ---------------------------------------------------------------------------

def assert_stale(verdict: Any, cite: str) -> list[dict[str, Any]]:
    """`POSSIBLY_STALE` with at least one witness — D1.13's contraposition:
    *`¬FRESH*(R, τ) ⇒ V(R, τ) = POSSIBLY_STALE`*."""
    assert not is_fresh(verdict), f"{cite}: FALSE FRESH — {describe(verdict)}"
    assert verdict_name(verdict) == "POSSIBLY_STALE", (
        f"{cite}: expected POSSIBLY_STALE, got {describe(verdict)}")
    ws = witnesses(verdict)
    assert ws, f"{cite}: POSSIBLY_STALE with no witness (D1.14, D13.27)"
    return ws


def assert_fresh(verdict: Any, cite: str) -> None:
    """A precision claim: this write provably cannot have changed the answer.

    Widening is always sound (D13.1), so these are the assertions that would
    rot silently if a derivation were coarsened — nothing else in the tree can
    fail when precision is lost.
    """
    assert is_fresh(verdict), f"{cite}: expected FRESH, got {describe(verdict)}"


def assert_matched(verdict: Any, conjunct: str, cite: str) -> None:
    """`matched_on` names the conjuncts that passed **non-vacuously** (M4 plan
    §3.5): a conjunct that passed because either side was `"*"` is not
    attribution, it is the absence of a narrowing. Only conjuncts that are
    concrete on both sides in the scenario at hand are asserted here."""
    assert conjunct in matched_on(verdict), (
        f"{cite}: witness should attribute the match to `{conjunct}` "
        f"(D13.27) — {describe(verdict)}")
