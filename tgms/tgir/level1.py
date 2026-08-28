"""`tgms/tgir/level1.py` — Level-1 witness narrowing (P1.3, first tranche).

`docs/design/M5_LEVEL1_SOUNDNESS.md` §1 (`PatternMatch`) and §4.3 fix the
contract this module implements; `M5_DESIGN.md` §6.2 fixes the invariant it
is written against:

> Level 1 lives in `tgms/tgir/level1.py`, strictly *downstream* of `check`.
> `check.py` gains no import. `level1` may only **remove** witnesses; it may
> never add one, and it may never touch `UNDECIDABLE`.

`refine()` is the one entry point. It consumes a `Verdict` that `check`/
`check_steps` already produced, plus the `ScanRegion` recorded for that same
step (or `None`/a malformed dict — the fail-safe, `M5_DESIGN.md` §4.3), and
returns a `Verdict` that is never more stale than the one it was given:

- `state != "possibly-stale"` (i.e. `"fresh"` or `"undecidable"`) passes
  through **unchanged, by identity** — `UNDECIDABLE` is never touched
  (D13.25: every consumer already reads it as `POSSIBLY_STALE`, so refining
  it would be refining a diagnosis, not a verdict) and a `FRESH` verdict has
  no witnesses to narrow.
- An absent or unparseable region (`scan_region_terms` returns `()`) is a
  no-op: `M5_DESIGN.md` §4.3's fail-safe, "an absent scan region means no
  narrowing", applies to every operator this module is ever handed, not only
  `PatternMatch`.
- A witness is dropped only when its own `OpFootprint` — rebuilt from the
  log, never trusted off the witness's own fields (the same "re-walk, do not
  trust the witness list" discipline `M5_LEVEL1_SOUNDNESS.md` §3.3 rule 2
  states for the cut-lined multi-hop item) — **fails to intersect every one**
  of the region's terms (`check.intersects`, MISS on all of them). A `REFUSE`
  on any term, or a footprint this module cannot rebuild at all, keeps the
  witness: nothing is ever dropped without a term/footprint pair proving the
  op cannot have mattered (`M5_LEVEL1_SOUNDNESS.md` §1.4's L-PM1).
- The verdict may become `FRESH` only when **every** witness is dropped
  **and** `Verdict.total` is fully accounted for by `Verdict.witnesses`
  (`total == len(witnesses)`, i.e. `WITNESS_CAP` truncated nothing) — the
  same accounting rule §3.3 rule 3 states for P-CLOSURE, applied here because
  a capped witness list is an incomplete sample and this module never
  re-walks the whole suffix on its own (`check` already did; re-walking here
  too would be the second, parallel algorithm §3.2 reason 2 forbids).
  Otherwise the verdict stays `POSSIBLY_STALE`, with `total` carried through
  unchanged (it is a fact about Level-0's scope, not about this narrowing)
  and only the witness list shrunk.
- A surviving witness's `matched_term`/`matched_on` are rewritten to name the
  region-derived term that actually explains the match — never the step's
  Level-0 term index, which would misdescribe *why* the op still matters
  once a narrower explanation is on hand. `tgms.artifact.witness` reads this
  to label the term "level-1" instead of "level-0" (§5.3).

This module never opens a store and never imports `tgms.temporal` or
`tgms.artifact` — the property `scripts/check_freshness_boundary.py` checks
mechanically. Its own allowlist: `tgms.core.model`, `tgms.core.errors`,
`tgms.storage.eventlog`, `tgms.tgir.depscope`, plus `tgms.tgir.footprint`,
`tgms.tgir.check` and `tgms.tgir.scan_region`.

**Scope note.** `docs/design/M5_LEVEL1_SOUNDNESS.md` §3 specifies a second,
much larger mechanism here — the multi-hop closure predicate P-CLOSURE, which
needs a state-carrying fixpoint over the whole suffix and cannot be expressed
as a second pass of `intersects` at all (§3.2). That item is the explicit
cut-line item of P1.3 and is **not** implemented by this module: `refine()`
below is exactly `docs/design/M5_LEVEL1_SOUNDNESS.md` table row 1 and row 2's
mechanism ("ships as ordinary `ScopeTerm`s") — a second, narrower pass of the
same `intersects` primitive `check` already uses, never a new algorithm.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from tgms.core.errors import InvalidArgError, StateError
from tgms.storage.eventlog import EventLog
from tgms.tgir.check import FRESH, POSSIBLY_STALE, Match, Verdict, Witness, intersects
from tgms.tgir.footprint import OpFootprint, footprints_of_batch
from tgms.tgir.scan_region import scan_region_terms

__all__ = ["refine"]


def refine(verdict: Verdict, region: Any, log: EventLog) -> Verdict:
    """Downstream of `check`. May only remove witnesses from `verdict`.

    `region` is whatever `StepDependency.scan_region` (or
    `annotations[node_digest]["scan_region"]`) held for the step this
    `verdict` was computed against — a `ScanRegion`, the raw dict form, or
    `None`. `log` is the same `EventLog` `check` was given; this function
    re-derives footprints from it and never trusts a `Witness`'s own fields
    as a substitute for the record they describe.
    """
    if verdict.state != "possibly-stale":
        return verdict  # FRESH and UNDECIDABLE pass through untouched, by identity

    terms = scan_region_terms(region)
    if not terms:
        return verdict  # absent/malformed/incomplete region: the fail-safe no-op

    batches = _batch_index(log)

    #: `matched_term`/`matched_on` are rewritten on every kept witness to
    #: name the region-derived term that explains it, not the step's Level-0
    #: term index — `tgms.artifact.witness._resolve` reads this to label the
    #: witness "level-1". A witness this loop cannot re-derive a footprint
    #: for, or that only REFUSEd, keeps its original (Level-0) indices: it
    #: was not narrowed, so it should not claim to have been.
    kept: list[Witness] = []
    for w in verdict.witnesses:
        fp = _footprint_for(w, batches)
        if fp is None:
            kept.append(w)  # cannot rebuild -> cannot prove exclusion -> keep
            continue
        hit, refused = _first_hit(terms, fp)
        if hit is not None:
            index, matched_on = hit
            kept.append(replace(w, matched_term=index, matched_on=matched_on))
        elif refused:
            kept.append(w)  # a term REFUSEd; that is not proof of exclusion
        # else: every term MISSed -> provably excluded (L-PM1) -> dropped

    if not kept and verdict.total == len(verdict.witnesses):
        # every op that intersected Level 0's scope is accounted for here
        # (no WITNESS_CAP truncation hid any) and every single one of them is
        # excluded by the narrower region -- safe to report FRESH.
        return FRESH(verdict.degraded, verdict.exempt)

    # either some witnesses remain, or the cap means an un-inspected op could
    # still matter -- state stays POSSIBLY_STALE either way, `total` is a
    # Level-0 fact and is carried through unchanged.
    return POSSIBLY_STALE(kept, verdict.total, verdict.degraded, verdict.exempt)


def _first_hit(terms: tuple[Any, ...],
               fp: OpFootprint) -> tuple[tuple[int, tuple[str, ...]] | None, bool]:
    """The first region term this footprint `intersects`, in term order —
    mirrors `check._scan_batch`'s own "first hit wins" `matched_term`
    convention — as `(hit, refused)`. `hit` is `None` when no term matched;
    `refused` is `True` when some term along the way returned `Match.REFUSE`
    (which is not proof of exclusion, so the caller must keep the witness
    rather than drop it)."""
    for index, term in enumerate(terms):
        m, matched_on = intersects(term, fp)
        if m is Match.REFUSE:
            return None, True
        if m is Match.HIT:
            return (index, matched_on), False
    return None, False


def _batch_index(log: EventLog) -> dict[str, dict[str, Any]]:
    """`{batch_id: batch}` over the whole log — log-only, one walk (§3.3 rule
    2, D13.20). A log this function cannot read yields an empty index, which
    makes every `_footprint_for` lookup miss and therefore keeps every
    witness — the same conservative direction as a footprint this module
    cannot rebuild for any other reason."""
    out: dict[str, dict[str, Any]] = {}
    try:
        for batch, _end, _raw in log.batches_from(0):
            bid = batch.get("batch_id")
            if isinstance(bid, str):
                out[bid] = batch
    except (StateError, OSError, ValueError):
        return {}
    return out


def _footprint_for(w: Witness, batches: dict[str, dict[str, Any]]) -> OpFootprint | None:
    """Re-derive `w`'s own `OpFootprint` from the batch it names —
    `(batch_id, op_seq, arm)` is exactly enough to pick one footprint back
    out of `footprints_of_batch` (D13.20: a footprint is derived from one
    logged op record and nothing else, so re-deriving it is re-reading the
    log, never re-trusting the witness)."""
    batch = batches.get(w.batch_id)
    if batch is None:
        return None
    try:
        fps = footprints_of_batch(batch)
    except (InvalidArgError, KeyError, TypeError, ValueError):
        return None
    for fp in fps.ops:
        if fp.seq == w.op_seq and fp.arm == w.arm:
            return fp
    return None
