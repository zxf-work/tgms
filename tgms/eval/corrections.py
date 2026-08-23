"""The correction generators M4's freshness harness injects (D6.3, plan §4.3).

Two dimensions, crossed. The **classes** come from FRESHNESS_SEMANTICS §2's
taxonomy of what the write API can actually produce; the **placement**
dimension is the freshness harness's own addition, because §3's
counterexamples show that placement is exactly what distinguishes a sound
dependency domain from an unsound one.

> *A harness that injects only `correct()` would score a mechanism sound that
> is unsound for appends* — the exact flaw §2.8 found already shipped in the
> tree. The five-class obligation is D6.3 and is a **gate**, not an aspiration.

The placement that matters most is `outside-window`: CE-4, FF-1 and RG-1 all
live there, and a value-arm-only mechanism returns `FRESH` and is wrong. The
`new-identity` placement is the class the naive row-touch baseline fails
outright — CE-1/CE-2/CE-3, where there are *no rows to touch*.

Everything here produces **logged op records** (`tgms.storage.base.make_op`),
applied as one batch, so a Class-E within-batch retirement is expressible and
so the harness's footprints are the real ones.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from tgms.core.model import OPEN_END
from tgms.storage.base import make_op

#: D6.3's five effect classes. `E` emits no footprint of its own (L2.1) and is
#: injected anyway, to prove that absence is not a hole.
CLASSES: tuple[str, ...] = ("A", "B", "C", "D", "E")

#: The generators, by the class they realize. Two classes need two generators
#: each because the frozen documents single out both settings by name: a
#: `correct` over a whole interval versus a sub-interval (CE-5 is the second),
#: and a `retract` whose `t` falls inside a believed version versus at or below
#: its start (only the first leaves a left replacement).
GENERATORS: dict[str, str] = {
    "a1_events": "A",
    "a2_disjoint": "A",
    "b_overwrite": "B",
    "c1_whole": "C",
    "c2_sub": "C",
    "d1_truncate": "D",
    "d2_full": "D",
    "e_within_batch": "E",
}

#: Four placements, plus the one that has no window at all.
PLACEMENTS: tuple[str, ...] = (
    "in-window-read",        # the easy true positive
    "in-window-unread",      # tests `I` narrowing
    "outside-window-read",   # THE CARVE CELL — CE-4, FF-1, RG-1 all live here
    "outside-window-unread",  # the true-negative cell
    "new-identity",          # CE-1/2/3 — the class row-touch cannot see
)


@dataclass(frozen=True, slots=True)
class Substrate:
    """What a store offers a generator, sampled once so 1,500 trials do not
    each rescan 35,000 edge versions."""

    uids: tuple[str, ...]
    rel_types: tuple[str, ...]
    vt_lo: int
    vt_hi: int
    node_label: str = "Node"

    @property
    def span(self) -> int:
        return max(1, self.vt_hi - self.vt_lo)


@dataclass(frozen=True, slots=True)
class Target:
    """What one `(Q, A)` cell read: the identities its arguments name, and its
    valid-time window if it takes one.

    `read_uids` is deliberately *the query's arguments*, not the rows it
    returned. D13.12 is explicit that a scope describes the **scan**, not the
    rows — and a harness that placed corrections by looking at returned rows
    would be building the row-touch rule into its own experimental design,
    which is the thing D6.4 exists to measure against.
    """

    read_uids: tuple[str, ...] = ()
    window: tuple[int, int] | None = None


@dataclass(frozen=True, slots=True)
class Correction:
    """One injected batch, fully described so a trial is replayable from its
    record alone."""

    cls: str
    generator: str
    placement: str
    ops: tuple[dict[str, Any], ...]
    note: str = ""
    identities: tuple[str, ...] = field(default=())

    def to_json(self) -> dict[str, Any]:
        return {"class": self.cls, "generator": self.generator,
                "placement": self.placement, "note": self.note,
                "identities": list(self.identities), "ops": list(self.ops)}


# ---------------------------------------------------------------------------
# sampling the substrate
# ---------------------------------------------------------------------------

def probe_substrate(store: Any, *, sample: int = 400,
                    rng: random.Random | None = None) -> Substrate:
    """Read enough of a store to place corrections in it, and no more."""
    rng = rng or random.Random(0)
    uids: list[str] = []
    rel_types: set[str] = set()
    lo, hi = OPEN_END, 0
    for v in store.adapter.all_node_versions():
        uids.append(v.uid)
        lo = min(lo, v.vt_s)
        if v.vt_e < OPEN_END:
            hi = max(hi, v.vt_e)
        if len(uids) >= sample * 8:
            break
    seen_edges = 0
    for e in store.adapter.all_edge_versions():
        rel_types.add(e.rel_type)
        lo = min(lo, e.vt_s)
        if e.vt_e < OPEN_END:
            hi = max(hi, e.vt_e)
        seen_edges += 1
        if seen_edges >= sample * 8:
            break
    uniq = sorted(set(uids))
    if len(uniq) > sample:
        uniq = rng.sample(uniq, sample)
    if hi <= lo:
        hi = lo + 1000
    return Substrate(tuple(sorted(uniq)), tuple(sorted(rel_types)) or ("R",),
                     lo if lo < OPEN_END else 0, hi)


# ---------------------------------------------------------------------------
# placement arithmetic
# ---------------------------------------------------------------------------

def _pick_uid(sub: Substrate, target: Target, placement: str,
              rng: random.Random) -> str | None:
    """Read versus unread, and the case where the distinction does not exist.

    An operator whose arguments name **no** identity — `version_history`,
    `aggregate_events`, `graph_metric_timeseries` — scans the whole store. For
    such a query *every* identity is a read identity and there is no unread
    one, which D13.12 says in the general form: a scope describes the scan, not
    the rows. So `-read` draws from anywhere and `-unread` yields nothing,
    rather than a fabricated distinction that would put true positives in the
    true-negative cell and quietly ruin the precision column.
    """
    if placement == "new-identity":
        return None
    named = set(target.read_uids)
    if not named:
        return rng.choice(list(sub.uids)) if (
            placement.endswith("-read") and sub.uids) else None
    if placement.endswith("-read"):
        pool = [u for u in target.read_uids if u in set(sub.uids)] or \
            list(target.read_uids)
        return rng.choice(pool) if pool else None
    unread = [u for u in sub.uids if u not in named]
    return rng.choice(unread) if unread else None


def _interval(sub: Substrate, target: Target, placement: str,
              rng: random.Random) -> tuple[int, int]:
    """Where in valid time the correction lands.

    With no window on the query — `entity_history` takes none (§9.1) — every
    placement is "in window" by construction, so the outside-window cells fall
    back to a region above the substrate's extent. That is *recorded* in the
    trial rather than silently collapsed: a cell that cannot be outside any
    window is not evidence about the carve arm.
    """
    step = max(2, sub.span // 20)
    if target.window is None or placement.startswith("in-window") \
            or placement == "new-identity":
        base = target.window[0] if target.window else sub.vt_lo
        top = target.window[1] if target.window else sub.vt_hi
        start = base + rng.randrange(max(1, (top - base) // 2)) if top > base else base
        return start, start + step
    # strictly above the window's right edge — a region the query did not read
    start = target.window[1] + 1 + rng.randrange(step)
    return start, start + step


def _fresh_uid(rng: random.Random) -> str:
    return f"__inj{rng.randrange(10 ** 9):09d}"


def _outside(target: Target, placement: str) -> bool:
    """Is this placement genuinely outside a real window?

    An operator that takes no window has no outside, and saying otherwise is
    how a cell gets mislabelled.
    """
    return target.window is not None and placement.startswith("outside-window")


def _version_for(believed: Sequence[Any], target: Target, placement: str,
                 vt_s: int, vt_e: int, rng: random.Random) -> Any | None:
    """Pick a believed version the correction can legally address **without
    abandoning its placement**.

    This is the function whose absence invalidated the first campaign's
    carve-arm measurement. `correct` and `retract` must hit a believed version
    or the write path refuses, and the obvious way to guarantee that — clamp
    the correction back onto the version's own interval — silently drags every
    outside-window Class B/C/D correction *inside* the query window. The cell
    then reports precision for corrections that were never outside anything,
    and the carve arm, whose entire purpose is to catch what the value arm's
    `vt` misses, never gets its chance to fire.

    So for an outside-window placement the version must genuinely reach past
    the window's right edge — which the event-stream shape `[first_seen, ∞)`
    does — and if none does, the cell is **not realizable** and returns `None`
    rather than a mislabelled substitute.
    """
    if not believed:
        return None
    if not _outside(target, placement):
        hits = [v for v in believed if v.vt_s < vt_e and vt_s < v.vt_e]
        return rng.choice(hits) if hits else rng.choice(list(believed))
    reaching = [v for v in believed if v.vt_e > target.window[1] and v.vt_s < vt_e]
    return rng.choice(reaching) if reaching else None


def _believed_nodes(store: Any, uid: str) -> list[Any]:
    try:
        return list(store.adapter.believed_node_versions(uid))
    except Exception:  # pragma: no cover - a backend that cannot answer
        return []


# ---------------------------------------------------------------------------
# the generators
# ---------------------------------------------------------------------------

def _a1_events(store, sub, target, placement, rng) -> Correction | None:
    """**Class A, appended events.** `ingest_events` supersedes nothing: every
    event without an explicit `disc` is its own logical edge (D2.1), so no row
    that already exists is touched. This is the generator the naive row-touch
    baseline is structurally unable to see."""
    vt_s, _vt_e = _interval(sub, target, placement, rng)
    rel = rng.choice(sub.rel_types)
    if placement == "new-identity":
        src, dst = _fresh_uid(rng), _fresh_uid(rng)
    else:
        anchor = _pick_uid(sub, target, placement, rng)
        if anchor is None:
            return None
        others = [u for u in sub.uids if u != anchor]
        src, dst = anchor, (rng.choice(others) if others else _fresh_uid(rng))
    return Correction(
        "A", "a1_events", placement,
        (make_op("ingest_events", offset=0, node_label=sub.node_label,
                 events=[{"src": src, "dst": dst, "rel_type": rel, "vt_s": vt_s}],
                 source="inject", provenance_ref=None),),
        note="appended event; supersedes nothing", identities=(src, dst))


def _a2_disjoint(store, sub, target, placement, rng) -> Correction | None:
    """**Class A, a disjoint interval.** CE-4's shape: an assert whose valid
    interval does not overlap any believed version, so it adds belief rather
    than replacing it — and, placed outside the query window, is exactly the
    correction a value-arm-only mechanism calls `FRESH`."""
    uid = _fresh_uid(rng) if placement == "new-identity" else \
        _pick_uid(sub, target, placement, rng)
    if uid is None:
        return None
    vt_s, vt_e = _interval(sub, target, placement, rng)
    believed = _believed_nodes(store, uid)
    closed = [v.vt_e for v in believed if v.vt_e < OPEN_END]
    if believed and not closed:
        # Every believed *node* version runs to `OPEN_END` — the shape every
        # event-stream store has, since `_ingest_events` writes
        # `[first_seen, ∞)`. There is no disjoint node interval to assert into,
        # so the disjoint append moves to an **edge**, whose versions are the
        # instantaneous `[vt_s, vt_s+1)` this class needs. Same class, same
        # placement, still CE-4's shape; falling back to `None` here would have
        # deleted Class-A-disjoint from both headline stores silently.
        others = [u for u in sub.uids if u != uid]
        dst = rng.choice(others) if others else _fresh_uid(rng)
        return Correction(
            "A", "a2_disjoint", placement,
            (make_op("assert_edge", src=uid, dst=dst,
                     rel_type=rng.choice(sub.rel_types), disc="a2-disjoint",
                     props={"injected": "a2"}, vt_s=vt_s, vt_e=vt_e,
                     source="inject", provenance_ref=None),),
            note="edge assert on a non-overlapping interval (node versions are "
                 "all open-ended here)", identities=(uid, dst))
    if closed:
        # step clear of every believed interval so this really is disjoint
        vt_s = max(vt_s, max(closed) + 1)
        vt_e = vt_s + max(2, sub.span // 20)
    return Correction(
        "A", "a2_disjoint", placement,
        (make_op("assert_node", uid=uid, label=sub.node_label,
                 props={"injected": "a2"}, vt_s=vt_s, vt_e=vt_e,
                 source="inject", provenance_ref=None),),
        note="assert on a non-overlapping interval", identities=(uid,))


def _b_overwrite(store, sub, target, placement, rng) -> Correction | None:
    """**Class B, an overwriting assert.** It replaces a whole version, so keys
    the new props *omit* also change — and `_remainder` re-inserts fragments at
    valid-time locations the op's own arguments do not bound, in **both**
    directions. That unbounded reach is what D13.21a's carve arm exists for."""
    uid = _fresh_uid(rng) if placement == "new-identity" else \
        _pick_uid(sub, target, placement, rng)
    if uid is None:
        return None
    vt_s, vt_e = _interval(sub, target, placement, rng)
    believed = _believed_nodes(store, uid)
    if believed:
        v = _version_for(believed, target, placement, vt_s, vt_e, rng)
        if v is None:
            return None
        # overlap the chosen version, but NEVER at the cost of the placement:
        # for outside-window the wanted interval is used as-is, which is legal
        # precisely because the version reaches OPEN_END
        if _outside(target, placement):
            if not (v.vt_s < vt_e and vt_s < v.vt_e):
                return None
        else:
            lo = max(v.vt_s, vt_s)
            hi = min(v.vt_e if v.vt_e < OPEN_END else lo + sub.span, vt_e)
            if hi <= lo:
                lo, hi = v.vt_s, v.vt_s + max(2, sub.span // 20)
            vt_s, vt_e = lo, hi
    return Correction(
        "B", "b_overwrite", placement,
        (make_op("assert_node", uid=uid, label=sub.node_label,
                 props={"injected": "b", "tier": "revised"},
                 vt_s=vt_s, vt_e=vt_e, source="inject", provenance_ref=None),),
        note="overwriting assert; carves", identities=(uid,))


def _correct(store, sub, target, placement, rng, *, whole: bool) -> Correction | None:
    """**Class C, a property correction.** The sub-interval form is CE-5's
    channel: it multiplies an identity's *events*, so an event-keyed operator
    that reads no property at all still changes."""
    if placement == "new-identity":
        return None                # a correct needs an existing believed version
    uid = _pick_uid(sub, target, placement, rng)
    if uid is None:
        return None
    believed = _believed_nodes(store, uid)
    if not believed:
        return None
    want_s, want_e = _interval(sub, target, placement, rng)
    v = _version_for(believed, target, placement, want_s, want_e, rng)
    if v is None:
        return None
    if _outside(target, placement):
        # the placement is the point: correct a sub-range that lies wholly
        # above the query window, which the version reaching OPEN_END makes
        # legal. Never clamped back onto the version's own start.
        vt_s = max(want_s, v.vt_s + 1 if v.vt_s >= want_s else want_s)
        vt_e = max(vt_s + 1, want_e if whole else vt_s + max(2, (want_e - want_s) // 2))
        vt_e = min(vt_e, v.vt_e)
        if vt_e <= vt_s:
            return None
        return Correction(
            "C", "c1_whole" if whole else "c2_sub", placement,
            (make_op("correct", ref={"kind": "node", "uid": uid},
                     props={"injected": "c", "revised": True},
                     vt_s=vt_s, vt_e=vt_e, source="inject", provenance_ref=None),),
            note=("whole-interval property correction, above the query window"
                  if whole else
                  "sub-interval property correction above the query window; "
                  "re-keys events (CE-5)"),
            identities=(uid,))
    # Stay inside ONE believed version. `_correct` refuses a multi-hit whose
    # versions disagree on `label` (D-140), and it is right to: `correct`
    # carries no label argument, so the corrected version can only inherit one
    # and an unordered scan would pick it. Clamping here keeps the generator
    # producing a Class-C correction rather than an exception.
    later = [w.vt_s for w in believed if w.vt_s > v.vt_s]
    ceiling = min(later) if later else OPEN_END
    top = min(v.vt_e if v.vt_e < OPEN_END else v.vt_s + max(4, sub.span // 4),
              ceiling)
    if whole:
        vt_s, vt_e = v.vt_s, top
    else:
        mid = v.vt_s + max(1, (top - v.vt_s) // 3)
        vt_s, vt_e = mid, min(top, mid + max(2, (top - v.vt_s) // 3))
    if vt_e <= vt_s:
        vt_e = vt_s + 1
    return Correction(
        "C", "c1_whole" if whole else "c2_sub", placement,
        (make_op("correct", ref={"kind": "node", "uid": uid},
                 props={"injected": "c", "revised": True},
                 vt_s=vt_s, vt_e=vt_e, source="inject", provenance_ref=None),),
        note=("whole-interval property correction" if whole else
              "sub-interval property correction; re-keys events (CE-5)"),
        identities=(uid,))


def _c1_whole(store, sub, target, placement, rng):
    return _correct(store, sub, target, placement, rng, whole=True)


def _c2_sub(store, sub, target, placement, rng):
    return _correct(store, sub, target, placement, rng, whole=False)


def _retract(store, sub, target, placement, rng, *, truncate: bool) -> Correction | None:
    """**Class D, a retraction.** `vt_s < t` truncates and leaves a left
    replacement (a rewritten row at a moved endpoint); `t <= vt_s` removes the
    version's coverage outright. Both remove belief over `[t, ∞)`, which is why
    the value arm is `vt_from(t)`."""
    if placement == "new-identity":
        return None
    uid = _pick_uid(sub, target, placement, rng)
    if uid is None:
        return None
    believed = _believed_nodes(store, uid)
    if not believed:
        return None
    want_s, want_e = _interval(sub, target, placement, rng)
    v = _version_for(believed, target, placement, want_s, want_e, rng)
    if v is None:
        return None
    top = v.vt_e if v.vt_e < OPEN_END else v.vt_s + max(4, sub.span // 2)
    if _outside(target, placement):
        # `t` above the window's right edge. `d2_full` has no outside form: its
        # whole definition is `t <= vt_s`, and a `vt_s` above the window would
        # mean the version never intersected the query at all.
        if not truncate:
            return None
        t = max(want_s, v.vt_s + 1)
        if not (v.vt_s < t < v.vt_e):
            return None
    elif truncate:
        t = v.vt_s + max(1, (top - v.vt_s) // 2)
        if not (v.vt_s < t < top):
            return None
    else:
        t = v.vt_s          # `t <= vt_s`: no left fragment survives
    return Correction(
        "D", "d1_truncate" if truncate else "d2_full", placement,
        (make_op("retract", ref={"kind": "node", "uid": uid}, t=int(t),
                 source="inject", provenance_ref=None),),
        note=("retract inside a believed interval; leaves a left replacement"
              if truncate else "retract at or below vt_s; removes coverage"),
        identities=(uid,))


def _d1_truncate(store, sub, target, placement, rng):
    return _retract(store, sub, target, placement, rng, truncate=True)


def _d2_full(store, sub, target, placement, rng):
    return _retract(store, sub, target, placement, rng, truncate=False)


def _e_within_batch(store, sub, target, placement, rng) -> Correction | None:
    """**Class E, within-batch retirement.** Two ops on one identity in one
    batch: the first version is written and retired at the same `tt`, so it was
    never believed (D-059) and L2.1 makes it provably harmless.

    **It emits no footprint of its own**, and the builder has no Class-E
    branch. Injecting it anyway is D6.3's requirement and the only way to show
    that the absence is a proof rather than a hole.
    """
    uid = _fresh_uid(rng) if placement == "new-identity" else \
        _pick_uid(sub, target, placement, rng)
    if uid is None:
        return None
    vt_s, vt_e = _interval(sub, target, placement, rng)
    if vt_e <= vt_s:
        vt_e = vt_s + 1
    common = dict(uid=uid, label=sub.node_label, vt_s=vt_s, vt_e=vt_e,
                  source="inject", provenance_ref=None)
    return Correction(
        "E", "e_within_batch", placement,
        (make_op("assert_node", props={"injected": "e", "gen": 1}, **common),
         make_op("assert_node", props={"injected": "e", "gen": 2}, **common)),
        note="two ops on one identity in one batch; the first is retired unbelieved",
        identities=(uid,))


_BUILDERS = {
    "a1_events": _a1_events, "a2_disjoint": _a2_disjoint,
    "b_overwrite": _b_overwrite, "c1_whole": _c1_whole, "c2_sub": _c2_sub,
    "d1_truncate": _d1_truncate, "d2_full": _d2_full,
    "e_within_batch": _e_within_batch,
}


def generate(store: Any, sub: Substrate, target: Target, *,
             rng: random.Random | None = None,
             generators: Sequence[str] | None = None,
             placements: Sequence[str] | None = None) -> list[Correction]:
    """The injection matrix for one `(Q, A)` cell.

    8 generators × 5 placements = 40 candidate cells, of which the realizable
    ones are returned — `correct` and `retract` need an existing believed
    version, so they have no `new-identity` form, and an operator taking no
    window has no outside-window form distinct from its in-window one (recorded
    in `_interval`, not hidden). That lands at **≈ 20 realized cells**, which
    is the plan's §4.3 count.

    A cell that cannot be realized returns nothing rather than a degraded
    substitute: a matrix that quietly swaps a Class-D retraction for a Class-B
    assert would report five classes while injecting four.
    """
    rng = rng or random.Random(0)
    out: list[Correction] = []
    for gen in (generators or list(_BUILDERS)):
        for placement in (placements or PLACEMENTS):
            built = _BUILDERS[gen](store, sub, target, placement, rng)
            if built is not None:
                out.append(built)
    return out


def classes_covered(corrections: Iterable[Correction]) -> set[str]:
    return {c.cls for c in corrections}


__all__ = [
    "CLASSES", "GENERATORS", "PLACEMENTS", "Correction", "Substrate", "Target",
    "classes_covered", "generate", "probe_substrate",
]
