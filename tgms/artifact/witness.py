"""`check_artifact` — the witness / refresh API, M5 design memo §5.

`check_artifact(record, log, tt_now=OPEN_END, *, chain_cache=None,
level1=True) -> ArtifactVerdict`. It reuses `check_steps` verbatim (via
`check_trace`, which already implements exactly the per-step-production /
merged-fallback selection §5.2 describes and calls `check_steps` unchanged —
duplicating that selection here would just be a second place for the two
to drift apart); it renders through `explain.render_steps`; it returns the
affected dependency *term*, a refresh handle, and — mandatorily —
D-153's exemption receipt. It exposes `.actionable_fresh` and no `is_stale`
(§5.6 — this module must never grow one).

Every `refresh.kind` / `RefreshHandle.kind` occurrence below names §5.5's own
closed `"tgir_plan" | "operator"` vocabulary for the record's refresh
mechanism — unrelated to `ArtifactRecord.kind`, §3.4's deliberately open,
never-branched-on classification of what an artifact *is*. This module never
reads `record.kind`; the two-different-things-sharing-a-word is the memo's
own naming, not a boundary this file crosses.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tgms.core.model import OPEN_END
from tgms.storage.eventlog import EventLog
from tgms.tgir.check import (
    WITNESS_CAP, ChainCache, StepsVerdict, UNDECIDABLE, Verdict, Witness, check_trace,
)
from tgms.tgir.explain import render_steps
from tgms.tgir.level1 import refine as level1_refine
from tgms.tgir.scan_region import scan_region_terms

from tgms.artifact.record import SCHEMA_VERSION, ArtifactId, ArtifactRecord

#: §1.4's known `plan_format` values. `witness.py` cannot import
#: `tgms.tgir.loader` — it is outside `scripts/check_freshness_boundary.py`'s
#: allowlist for this module (§7.1) — so the recognized set is restated here
#: rather than imported. Duplicates `tgms/tgir/loader.py:42`'s `PLAN_FORMAT`;
#: keep the two in sync by hand until a version 2 exists to test the seam.
KNOWN_PLAN_FORMATS: frozenset[int] = frozenset({1})


@dataclass(frozen=True, slots=True)
class TermRef:
    """§5.3 — a `Witness.matched_term` index, resolved to the actual term.

    `level` disaggregates a Level-0 witness from a scan-region-derived one
    (P1.3, `tgms.tgir.level1`). A witness belonging to a step whose
    `StepDependency.scan_region` produced at least one usable `ScopeTerm`
    (`scan_region_terms(...)` non-empty) is `"level-1"`, and `term` is that
    narrower, region-derived `ScopeTerm` — never the step's stored Level-0
    term, which would misdescribe *why* the op still matters once a narrower
    explanation exists. Every other witness — no region recorded, a region
    the fail-safe could not interpret, or `level1=False` on `check_artifact`
    — is `"level-0"`, exactly as before P1.3 landed.
    """

    step_id: str | None
    index: int
    term: dict[str, Any]
    matched_on: tuple[str, ...]
    level: str

    def to_json(self) -> dict[str, Any]:
        return {"step_id": self.step_id, "index": self.index, "term": self.term,
                "matched_on": list(self.matched_on), "level": self.level}


@dataclass(frozen=True, slots=True)
class ExemptReceipt:
    """D-153's mandatory receipt, one per step that exempted at least one
    batch (`check.py`'s `Verdict.exempt`, carried through verbatim)."""

    step_id: str | None
    basis: int | None
    batches: int
    tt_range: tuple[int | None, int | None]
    theorem: str

    def to_json(self) -> dict[str, Any]:
        return {"step_id": self.step_id, "basis": self.basis, "batches": self.batches,
                "tt_range": list(self.tt_range), "theorem": self.theorem}


@dataclass(frozen=True, slots=True)
class RefreshHandle:
    """§5.5 — constructed and returned, never executed. `check_artifact`
    does not call it, does not import `tgms.artifact.refresh`, and does not
    open a store."""

    artifact: ArtifactId
    kind: str            # "tgir_plan" | "operator" — §1.4/§5.5's vocabulary
    ref: str
    plan_format: int | None
    basis_policy: str

    def to_json(self) -> dict[str, Any]:
        return {"artifact": self.artifact.to_json(), "kind": self.kind, "ref": self.ref,
                "plan_format": self.plan_format, "basis_policy": self.basis_policy}


@dataclass(frozen=True, slots=True)
class ArtifactVerdict:
    """§5.2's shape."""

    artifact: ArtifactId
    steps: StepsVerdict
    terms: tuple[TermRef, ...]
    exempt: tuple[ExemptReceipt, ...]
    refresh: RefreshHandle | None

    @property
    def actionable_fresh(self) -> bool:
        """Mirrors `check.py:381-385` / `StepsVerdict.actionable_fresh`. The
        **only** affirmative question — this type grows no `is_stale`."""
        return self.steps.actionable_fresh

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"artifact": self.artifact.to_json(), **self.steps.to_json()}
        if self.terms:
            out["terms"] = [t.to_json() for t in self.terms]
        # §5.4: printed even when empty would be misleading either way, so
        # the mandatory receipt is emitted whenever it is non-empty and the
        # verdict is otherwise silent about it — the CLI (§5.4 point 2)
        # is what prints it unconditionally, including on FRESH.
        if self.exempt:
            out["exempt"] = [e.to_json() for e in self.exempt]
        if self.refresh is not None:
            out["refresh"] = self.refresh.to_json()
        return out


def _term_map(record: ArtifactRecord) -> dict[str | None, tuple[dict[str, Any], ...]]:
    """The same per-step-production/merged-fallback selection `check_trace`
    (`check.py:852-864`) makes, restated only far enough to know which
    term list a given `step_id` resolves against — `check_trace` itself
    does not hand back that mapping, and rebuilding the verdict just to get
    it would be wasteful."""
    if record.steps:
        return {s.step_id: tuple(s.dependency.to_json()["terms"]) for s in record.steps}
    if record.dependency is not None:
        return {"plan": tuple(record.dependency.to_json()["terms"])}
    return {}


def _resolve(term_map: dict[str | None, tuple[dict[str, Any], ...]],
            region_terms: dict[str | None, tuple[Any, ...]], w: Witness) -> TermRef:
    """A witness whose `step_id` appears in `region_terms` came out of
    `level1.refine` against a non-empty scan region — `w.matched_term` then
    indexes *that* term list (`level1.refine` rewrites it on every witness it
    keeps, never trusting the Level-0 index once a narrower one is on hand),
    and the term shown is the region-derived one, `level="level-1"`. Every
    other witness resolves against the stored Level-0 scope exactly as
    before P1.3 (`level="level-0"`)."""
    region = region_terms.get(w.step_id)
    if region is not None:
        term = region[w.matched_term].to_json() if 0 <= w.matched_term < len(region) else {}
        return TermRef(step_id=w.step_id, index=w.matched_term, term=term,
                       matched_on=w.matched_on, level="level-1")
    terms = term_map.get(w.step_id, ())
    term = terms[w.matched_term] if 0 <= w.matched_term < len(terms) else {}
    return TermRef(step_id=w.step_id, index=w.matched_term, term=term,
                   matched_on=w.matched_on, level="level-0")


def _apply_level1(steps_verdict: StepsVerdict, record: ArtifactRecord, log: EventLog,
                  ) -> tuple[StepsVerdict, dict[str | None, tuple[Any, ...]]]:
    """The `level1` wiring seam (`M5_DESIGN.md` §6.2; `M5_LEVEL1_SOUNDNESS.md`
    §4.2). Per step: if `StepDependency.scan_region` is absent, or present but
    `scan_region_terms` cannot read a single term out of it (the fail-safe —
    W-P1..W-P6, or a future/unrecognized schema), the step's `Verdict` passes
    through **completely untouched** — not even re-constructed — which is
    what makes the absent-region case byte-identical to `level1=False`
    (M5_LEVEL1_SOUNDNESS.md §1.8 test 9). Otherwise `level1.refine` runs,
    strictly downstream of the `check`/`check_steps` call that already
    produced `steps_verdict` — this function never re-derives a verdict, it
    only narrows one that already exists.
    """
    regions = {s.step_id: s.scan_region for s in record.steps}
    region_terms: dict[str | None, tuple[Any, ...]] = {}
    refined: list[tuple[str, Verdict]] = []
    for sid, verdict in steps_verdict.per_step:
        region = regions.get(sid)
        terms = scan_region_terms(region) if region is not None else ()
        if not terms:
            refined.append((sid, verdict))
            continue
        region_terms[sid] = terms
        refined.append((sid, level1_refine(verdict, region, log)))
    return StepsVerdict(tuple(refined)), region_terms


def _refresh_handle(record: ArtifactRecord) -> RefreshHandle | None:
    """§1.4 / §5.5: an unrecognized `plan_format` refuses the refresh and
    leaves the verdict untouched — `handle` is `None`, nothing else changes."""
    refresh_kind = record.refresh["kind"]
    ref = record.refresh["ref"]
    basis_policy = record.refresh.get("basis_policy", "open")
    if refresh_kind == "operator":
        # an opaque-leaf artifact's (op, bound_args) document — no loader.py
        # version concept applies (§1.4).
        return RefreshHandle(record.id, refresh_kind, ref, None, basis_policy)
    plan_format = record.plan.get("plan_format")
    if plan_format not in KNOWN_PLAN_FORMATS:
        return None
    return RefreshHandle(record.id, refresh_kind, ref, int(plan_format), basis_policy)


def check_artifact(record: ArtifactRecord, log: EventLog, tt_now: int = OPEN_END, *,
                   chain_cache: ChainCache | None = None, level1: bool = True,
                   witness_cap: int = WITNESS_CAP) -> ArtifactVerdict:
    """§5.1. Reuses `check_steps` verbatim (through `check_trace`); renders
    through `explain.render_steps`; returns the affected dependency terms, a
    non-executing refresh handle, and D-153's exemption receipt — mandatory,
    present-but-empty when nothing was exempted.

    **`level1` (P1.3, `tgms.tgir.level1.refine`, wired here).** `level1=True`
    (the default) runs every step's `Verdict` through `_apply_level1` *after*
    `check_trace` has already produced it — strictly downstream of `check`,
    per `M5_DESIGN.md` §6.2, and this function never re-derives a verdict on
    its own account. A step with no recorded `scan_region`, or one
    `scan_region_terms` cannot read a term out of, passes through completely
    untouched (§4.3's fail-safe: "an absent scan region means no narrowing"),
    which is what makes `level1=True` and `level1=False` produce
    byte-identical output whenever no step carries a usable region —
    verified for the always-no-region case by
    `tests/test_artifact_check.py::
    test_level1_flag_is_wired_and_absent_region_is_a_no_op`,
    and for the PatternMatch case by `tests/test_scan_region_pattern.py`.
    `level1=False` skips `_apply_level1` outright, so every witness and term
    resolves as Level-0 regardless of what was recorded — the caller's own
    opt-out, independent of the fail-safe.

    A record whose own schema `version` is unrecognized is refused outright
    (§1.3's "a reader that does not recognize the version must refuse, never
    report fresh"), mirroring `check.py`'s own step 1 rather than
    `ArtifactRecord`'s constructor — exactly where `DependencyScope`'s
    version gate lives too.
    """
    if record.version != SCHEMA_VERSION:
        steps_verdict = StepsVerdict((("record", UNDECIDABLE("scope-version")),))
    else:
        steps_verdict = check_trace(record.to_json(), log, tt_now,
                                    chain_cache=chain_cache, witness_cap=witness_cap)

    region_terms: dict[str | None, tuple[Any, ...]] = {}
    if level1 and record.version == SCHEMA_VERSION:
        steps_verdict, region_terms = _apply_level1(steps_verdict, record, log)

    term_map = _term_map(record) if record.version == SCHEMA_VERSION else {}
    terms = tuple(_resolve(term_map, region_terms, w) for w in steps_verdict.witnesses)
    exempt = tuple(
        ExemptReceipt(step_id=sid, basis=v.exempt["basis"], batches=v.exempt["batches"],
                     tt_range=(v.exempt["tt_range"][0], v.exempt["tt_range"][1]),
                     theorem=v.exempt["theorem"])
        for sid, v in steps_verdict.per_step if v.exempt is not None
    )
    refresh = _refresh_handle(record) if record.version == SCHEMA_VERSION else None
    return ArtifactVerdict(record.id, steps_verdict, terms, exempt, refresh)


def render_verdict(verdict: ArtifactVerdict, *, produced_tt: int | None = None) -> str:
    """§5.6: `render_steps` reused unchanged, plus one line naming the
    artifact and its generation, plus one line per exemption receipt."""
    lines = [f"{verdict.artifact.name}@{verdict.artifact.generation}",
             render_steps(verdict.steps, produced_tt=produced_tt)]
    for e in verdict.exempt:
        step = f" (step {e.step_id})" if e.step_id else ""
        lines.append(f"exempt{step}: basis={e.basis} batches={e.batches} "
                     f"tt_range={list(e.tt_range)} theorem={e.theorem}")
    return "\n".join(lines)


__all__ = [
    "KNOWN_PLAN_FORMATS", "ArtifactVerdict", "ExemptReceipt", "RefreshHandle", "TermRef",
    "check_artifact", "render_verdict",
]
