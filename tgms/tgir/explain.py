"""Rendering a verdict as something a person can act on (D13.27, memo §14).

The whole mechanism exists so that a reader of a stored answer can be told, in
one sentence, that it may no longer hold and why. D13.27 fixes the shape:

> *"This answer was produced on March 1. A correction received on March 8
> revised node A over the period this computation read. Reconsider."*

Three things in that sentence come straight off a witness — `tt` rendered as a
wall-clock instant, `identity` naming the corrected thing, and the fact that it
overlapped what the computation read — and the last word is the point: the
mechanism **never repairs and never asserts a new answer**. It says reconsider.

Two rules this module keeps:

- **`UNDECIDABLE` is rendered as "may be stale", never as "unknown"**
  (D13.25). Every consumer treats it as `POSSIBLY_STALE`; a rendering that
  offers a third mood invites a reader to treat "don't know" as "probably
  fine", which is the one inference the contract forbids.
- **No number is invented.** Where the witness list was capped, the sentence
  says how many there really were.

Like `check.py`, this reads a verdict and nothing else — no store, no adapter.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from tgms.tgir.check import StepsVerdict, Verdict, Witness

#: `tt` is microseconds since the epoch throughout the store (HLC-derived).
_TT_PER_SECOND = 1_000_000

_REASON_TEXT: dict[str, str] = {
    "scope-version": "this answer's dependency record is from a newer version of "
                     "the format than this reader understands",
    "unknown-enum": "this answer's dependency record names something this reader "
                    "does not recognize",
    "store-mismatch": "this answer was not produced against this store",
    "no-tt_q": "this answer records no belief timestamp, so there is no point to "
               "compare a correction against",
    "log-rewritten": "this store's history no longer matches the one this answer "
                     "was produced from",
    "log-unreadable": "this store's history could not be read",
}


def render_tt(tt: int) -> str:
    """A transaction time as a UTC instant. `OPEN_END` and other saturating
    values are left as-is rather than rendered as a date in the year 148471."""
    try:
        return datetime.fromtimestamp(tt / _TT_PER_SECOND, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC")
    except (OverflowError, OSError, ValueError):  # pragma: no cover - defensive
        return str(tt)


def render_identity(identity: dict[str, Any]) -> str:
    """*"node A"*, *"the edge A→B (MSG)"*, *"2 nodes"* — what the correction
    touched, named the way a person would name it."""
    uid = identity.get("uid")
    if uid is not None:
        if isinstance(uid, list):
            return f"node {uid[0]}" if len(uid) == 1 else f"{len(uid)} nodes"
        return f"node {uid}"
    src, dst = identity.get("src"), identity.get("dst")
    rel = identity.get("rel_type")
    if src is None and dst is None:
        return "an edge"

    def one(value: Any) -> str:
        if isinstance(value, list):
            return value[0] if len(value) == 1 else f"{len(value)} endpoints"
        return str(value)

    kind = f" ({one(rel)})" if rel is not None else ""
    return f"the edge {one(src)}→{one(dst)}{kind}"


def render_witness(w: Witness, *, produced_tt: int | None = None) -> str:
    """D13.27's sentence, from one witness."""
    what = render_identity(w.identity)
    verb = {"assert_node": "revised", "assert_edge": "revised",
            "correct": "corrected", "retract": "retracted",
            "ingest_events": "added events touching"}.get(w.kind, "changed")
    where = ("over a valid-time region this computation read"
             if w.arm == "value"
             else "in a way whose valid-time reach this store cannot bound")
    lead = (f"This answer was produced on {render_tt(produced_tt)}. "
            if produced_tt is not None else "")
    step = f" (step {w.step_id})" if w.step_id else ""
    return (f"{lead}A write received on {render_tt(w.tt)} {verb} {what} "
            f"{where}{step}. Reconsider.")


def render(verdict: Verdict, *, produced_tt: int | None = None) -> str:
    """One paragraph for a whole verdict.

    `UNDECIDABLE` renders as *may be stale* with its reason attached — never as
    a third mood (D13.25).
    """
    if verdict.actionable_fresh:
        note = (f" (checked with a widened scan: {', '.join(verdict.degraded)})"
                if verdict.degraded else "")
        lead = (f"This answer was produced on {render_tt(produced_tt)}. "
                if produced_tt is not None else "")
        return (f"{lead}Nothing written since could have changed it{note}.")

    if verdict.state == "undecidable":
        why = _REASON_TEXT.get(verdict.reason or "", "this answer could not be checked")
        return (f"This answer may be stale and could not be checked: {why}. "
                f"Treat it as unverified.")

    lines = [render_witness(w, produced_tt=produced_tt if i == 0 else None)
             for i, w in enumerate(verdict.witnesses)]
    if verdict.total > len(verdict.witnesses):
        lines.append(f"{verdict.total - len(verdict.witnesses)} further writes "
                     f"also intersected this answer's dependencies "
                     f"({verdict.total} in total).")
    return "\n".join(lines)


def render_steps(verdict: StepsVerdict, *, produced_tt: int | None = None) -> str:
    """A plan's verdict: the headline bit, then per-step attribution.

    The plan verdict is one bit (D5.4) — but each witness names the step it
    actually hit, and that attribution is what diagnoses *which* operator's
    scope is loose, so it is never collapsed away here.
    """
    if verdict.actionable_fresh:
        return render(next((v for _s, v in verdict.per_step), Verdict("fresh")),
                      produced_tt=produced_tt)
    head = "This answer may be stale."
    body = []
    for sid, v in verdict.per_step:
        if v.actionable_fresh:
            continue
        detail = render(v).replace("\n", "\n    ")
        body.append(f"  {sid}: {detail}")
    return "\n".join([head, *body])


__all__ = ["render", "render_identity", "render_steps", "render_tt", "render_witness"]
