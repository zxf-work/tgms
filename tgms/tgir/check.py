"""`intersects` and `check` — the freshness question, answered (D13.23–D13.27).

*"Could anything written since have changed this?"* — asked of a stored
`DependencyScope` and an `EventLog`, and answered without recomputing anything.

Three shapes govern this module:

- **`intersects` is three-valued, not boolean** (D13.23a). An unrecognized enum
  anywhere in a term must produce `UNDECIDABLE`, never `False`. A function
  returning `bool` cannot express that, and FF-8 is what happens when the
  distinction is lost: `role: "both"` was a real scan mode with no encoding,
  an implementer wrote the term down faithfully, and got one that matched
  nothing. So the return is `Match ∈ {HIT, MISS, REFUSE}` and `check` lifts a
  single `REFUSE` to `UNDECIDABLE` for the whole scope.
- **`UNDECIDABLE` is not a third contract** (D13.25). Every consumer treats it
  as `POSSIBLY_STALE`; it is separated only so a diagnosis is not lost inside a
  conservative verdict. `Verdict` therefore exposes `.actionable_fresh` rather
  than inviting a caller to compare against an enum, because `verdict !=
  POSSIBLY_STALE` is the shape of the bug.
- **No *whole-scope* short-circuit is licensed by `pinned = true`** (D13.24,
  FF-4). A genuinely pinned scope scans a suffix that is empty *because the
  log says so*, not because a flag said to skip the scan. No step 1-7 is ever
  skipped on the strength of `pinned`, and a test asserts it.
- **Step 8a (§15 2026-08-27 entry E-10) is a per-batch exemption, not a
  scope-level short-circuit.** Inside step 8's loop, before a batch's
  footprints are built, a batch is exempted from testing — recorded in a
  receipt, never silently — when the scope carries `as_of_tt` (opt-in,
  additive; absent means no exemption), `tt_q_verified` is true, and
  `batch.tt > as_of_tt` (T1's four-step proof restricted to one batch). Every
  batch is still enumerated and every other step still runs in full; this is
  what distinguishes it from FF-4's `if pinned: return FRESH`.
- **Step 7b refuses on a known overlap** (failure ledger
  `check-assumed-non-overlap-precondition`, pre-registered as P-ENG2). Every
  footprint and every scope derivation assumes I1 — no two believed versions
  of one identity overlap in valid time — and `ingest_events` is the one
  applier that does not enforce it (it supersedes nothing, so an event whose
  explicit `disc` names an existing edge sits beside it). When the log, replayed
  over intervals alone, leaves such a pair believed for an identity the scope
  could have read, the verdict is `UNDECIDABLE("known-overlap")`: the
  precondition the other steps rest on is known to be false there.

**This module reads a log and a scope. It never reads a store** — D13.20's
restriction, enforced by `scripts/check_freshness_boundary.py`.
"""

from __future__ import annotations

import hashlib
import os
import threading
import weakref
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Iterable, Iterator, Literal, Sequence

from tgms.core.errors import InvalidArgError, StateError
from tgms.core.model import OPEN_END, Interval, edge_eid
from tgms.storage.eventlog import SEED_CHAIN, EventLog, extend_chain
from tgms.tgir.depscope import (
    INCIDENT_ROLES, KINDS, PSEUDO_PROPS, TOP, TOP_JSON, UNANCHORED, VT_MODES,
    DependencyScope, EdgeKey, Incident, ScopeTerm, StoreIdentityMemo, Targets,
    store_identity,
)
from tgms.tgir.footprint import (
    BatchFootprint, Identity, OpFootprint, footprints_of_batch,
)

# ---------------------------------------------------------------------------
# the three-valued match
# ---------------------------------------------------------------------------


class Match(Enum):
    """D13.23a's totality, as a type. `meets` is total over recognized values
    and undefined nowhere: it either matches, or fails to match on a value it
    understands, or **refuses**."""

    HIT = "hit"
    MISS = "miss"
    REFUSE = "refuse"


#: The five conjuncts, in the order a witness reports them (§13.6's spelling).
#: Evaluation order is different and is cheap-first — see `intersects`.
CONJUNCTS: tuple[str, ...] = ("kinds", "targets", "rel_types", "vt", "props")

#: The seven reasons `check` can refuse with (D13.24). The list is closed: a
#: new one means the frozen algorithm grew a step, which is a §9 escalation
#: rather than a commit. The seventh, `known-overlap` (step 7b), is exactly
#: that: pre-registered engine change P-ENG2 (2026-10-30), failure ledger
#: `check-assumed-non-overlap-precondition`.
REASONS: tuple[str, ...] = (
    "scope-version", "unknown-enum", "store-mismatch", "no-tt_q",
    "log-rewritten", "log-unreadable", "known-overlap",
)

#: Presentation cap on witnesses **per step**, with the true count always
#: retained alongside (D13.27, plan §9.8). Chosen at 20: enough that a human
#: reading a verdict sees the shape of what fired — several distinct ops,
#: usually several distinct terms — and few enough that a broad scope over a
#: long suffix does not turn one verdict into a megabyte of JSON.
#:
#: **The cap is a presentation limit and can never change the verdict**, and
#: **it is applied after `matched_on` accounting**, or §4.6's per-arm precision
#: numbers would be silently truncated along with the witness list.
WITNESS_CAP = 20


def _as_set(value: Any) -> frozenset[Any]:
    if isinstance(value, (tuple, list, set, frozenset)):
        return frozenset(value)
    return frozenset({value})


def meets(a: Any, b: Any) -> bool:
    """`a == "*" ∨ b == "*" ∨ as_set(a) ∩ as_set(b) ≠ ∅` — the single
    primitive (D13.23).

    **Every field on both sides is a set**; a scalar is its singleton. There is
    no scalar membership operator in this contract, because the coarsened arms
    of D13.22 carry lists and a scalar-typed test silently returns false
    against them (CO-8) — a false negative, and therefore unsound.
    """
    if a is TOP or b is TOP:
        return True
    return bool(_as_set(a) & _as_set(b))


def _vacuous(a: Any, b: Any) -> bool:
    """Did this conjunct pass because either side was `"*"`?

    A conjunct that passed on a `"*"` is not attribution, it is *absence of
    narrowing*, and listing it in `matched_on` would make every witness name
    all five conjuncts — which makes §13.10's "measure the carve arm's cost, do
    not estimate it" unmeasurable. The frozen text spells this two ways for the
    same match (D13.27's specimen against §13.6's verdict block); the
    coordinator ruled for §13.6's spelling.
    """
    return a is TOP or b is TOP


def vt_overlaps(a: Any, b: Any) -> bool:
    """Plain half-open overlap — `a_s < b_e ∧ b_s < a_e` — with **no
    adjustment on either side**, because D13.21 already did it, once, on the
    footprint side. `"*"` on either side overlaps everything, which is how the
    carve arm reaches a narrow-`vt` scope."""
    if a is TOP or b is TOP:
        return True
    return any(a_s < b_e and b_s < a_e for a_s, a_e in a for b_s, b_e in b)


def _entry_match(member: Any, fp: OpFootprint) -> Match:
    """E-4(b) — `entry_match`, the rule D13.23 left out for the `edges` arm's
    object form. Three branches, each erring in the safe direction:

    - **a field the `EdgeKey` omits is a wildcard** and is not tested;
    - **a field the *footprint* does not carry is treated as `"*"`**, i.e. it
      matches. This is the branch that matters: `ingest_events`' edge arm
      coarsens `disc` away (D13.22), so an `EdgeKey` naming `disc` must still
      meet a footprint that has none. Returning `False` there would be a **false
      negative**; unknown-as-`"*"` is the widening direction (D13.1);
    - **an unrecognized entry form returns `UNDECIDABLE`**, never a non-match —
      D13.23a, the rule FF-8 exists to have installed.

    No current derivation emits an `EdgeKey`, so this is spec completion rather
    than a live path. It is written down so that the day one does, the answer
    is not invented at the call site.
    """
    if isinstance(member, str):
        eids = fp.eids
        # E-1 gives every edge arm an `eid` set, so this is now precise. Were
        # one ever absent, the same unknown-as-`"*"` rule applies.
        return Match.HIT if eids is None or meets((member,), eids) else Match.MISS
    if isinstance(member, EdgeKey):
        for name in ("src", "dst", "rel_type", "disc"):
            want = getattr(member, name)
            if want is None:
                continue           # a field the key omits is a wildcard
            have = getattr(fp.identity, name)
            if have is None:
                continue           # a field the footprint lacks is a wildcard
            if not meets(want, have):
                return Match.MISS
        return Match.HIT
    return Match.REFUSE


def _edges_arm_match(edges: Any, fp: OpFootprint) -> Match:
    """`edges_match(E, fp) = ∃ e ∈ E : entry_match(e, fp)` (E-4(b)). An absent
    arm is ∅ and never reaches here; `"*"` is everything."""
    if edges is TOP:
        return Match.HIT
    hit = False
    for member in edges:
        m = _entry_match(member, fp)
        if m is Match.REFUSE:
            return m
        if m is Match.HIT:
            hit = True
    return Match.HIT if hit else Match.MISS


def incident_match(incident: Any, fp: OpFootprint) -> Match:
    """D13.23's incidence rule. **The load-bearing arm** (D13.4): an
    endpoint-scoped term matches an edge write whose `eid` did not exist when
    the scope was written, which is what CE-1/CE-2/CE-3 turn on."""
    if incident is None:
        return Match.MISS          # an absent arm is ∅, not ⊤
    if incident is TOP:
        return Match.HIT
    if not isinstance(incident, Incident):
        return Match.REFUSE
    role, uids = incident.role, incident.uids
    src, dst = fp.identity.src, fp.identity.dst
    if role == "src":
        return Match.HIT if src is not None and meets(uids, src) else Match.MISS
    if role == "dst":
        return Match.HIT if dst is not None and meets(uids, dst) else Match.MISS
    if role == "either":
        ok = ((src is not None and meets(uids, src))
              or (dst is not None and meets(uids, dst)))
        return Match.HIT if ok else Match.MISS
    if role == "both":
        ok = (src is not None and dst is not None
              and meets(uids, src) and meets(uids, dst))
        return Match.HIT if ok else Match.MISS
    return Match.REFUSE            # never False — D13.23a


def targets_match(targets: Any, fp: OpFootprint) -> tuple[Match, str | None]:
    """The entity-kind routing, and which arm fired.

    **An absent arm means ∅ for that entity kind.** A scope with only a `nodes`
    arm never matches an edge write, and vice versa — which is why `None` and
    `TOP` are different values in `Targets` and why this cannot be written as a
    default-to-`"*"` lookup.
    """
    if targets is TOP:
        return Match.HIT, None
    if not isinstance(targets, Targets):
        return Match.REFUSE, None
    if fp.entity_kind == "node":
        if targets.nodes is None:
            return Match.MISS, None
        uid = fp.identity.uid
        if uid is not None and meets(targets.nodes, uid):
            return Match.HIT, (None if _vacuous(targets.nodes, uid) else "targets.nodes")
        return Match.MISS, None
    # an edge footprint: identity **or** endpoint — a disjunction
    if targets.edges is not None:
        m = _edges_arm_match(targets.edges, fp)
        if m is Match.REFUSE:
            return m, None
        if m is Match.HIT:
            return m, (None if targets.edges is TOP else "targets.edges")
    m = incident_match(targets.incident, fp)
    if m is Match.REFUSE:
        return m, None
    if m is Match.HIT:
        vacuous = targets.incident is TOP or (
            isinstance(targets.incident, Incident) and targets.incident.uids is TOP)
        return m, (None if vacuous else "targets.incident")
    return Match.MISS, None


def intersects(term: ScopeTerm, fp: OpFootprint) -> tuple[Match, tuple[str, ...]]:
    """D13.23's five conjuncts, plus the `matched_on` accounting.

    Evaluated **cheap-first** — `kinds`, then the entity-kind-guarded
    `rel_types`, then `vt` and `props`, and `targets_match` last because it is
    the expensive one — and *reported* in §13.6's order, which is a different
    thing and is why the two orders are written down separately.

    Three of D13.23's four named properties live here and each is a bug someone
    would otherwise introduce:

    1. **`rel_types` is consulted only for edge footprints.** A node write has
       no relation type; testing it against a rel-type-restricted scope would
       either match everything (a precision disaster) or nothing (**unsound**).
       This guard is what keeps `aggregate_events(rel_types=["MSG"])` from
       being invalidated by every unrelated node assert.
    2. **The three target arms are a disjunction, and an absent arm is ∅.**
    3. **`vt_overlaps` is plain half-open overlap with no adjustment.**

    The fourth — *each arm is matched separately, never a merged
    pseudo-footprint* — is `check`'s, because it is about what gets passed in.
    """
    fired: dict[str, str] = {}

    if not meets(term.kinds, (fp.kind,)):
        return Match.MISS, ()
    # `fp.kind` is always one concrete kind, so this conjunct is vacuous
    # exactly when the term's side is `"*"`
    if term.kinds is not TOP:
        fired["kinds"] = "kinds"

    if fp.entity_kind == "edge":
        if not meets(term.rel_types, fp.rel_type):
            return Match.MISS, ()
        if not _vacuous(term.rel_types, fp.rel_type):
            fired["rel_types"] = "rel_types"

    if not vt_overlaps(term.vt, fp.vt):
        return Match.MISS, ()
    if not _vacuous(term.vt, fp.vt):
        fired["vt"] = "vt"

    if not meets(term.props, fp.props):
        return Match.MISS, ()
    if not _vacuous(term.props, fp.props):
        fired["props"] = "props"

    m, arm = targets_match(term.targets, fp)
    if m is not Match.HIT:
        return m, ()
    if arm:
        fired["targets"] = arm

    return Match.HIT, tuple(fired[c] for c in CONJUNCTS if c in fired)


# ---------------------------------------------------------------------------
# the witness (D13.27)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Witness:
    """What `POSSIBLY_STALE` returns, per intersecting op.

    It carries exactly what the three consumers need: `tt` and `identity` for
    the user-facing message, `kind` + `arm` + `matched_on` for the precision
    accounting (`class` cannot carry it — A-vs-B is not log-derivable, CO-3),
    and `batch_id` + `tt` for **audit**, so a witness is *checkable against the
    log* rather than trusted.
    """

    batch_id: str
    tt: int
    op_seq: int
    arm: str
    cls: str
    kind: str
    identity: dict[str, Any]
    vt: Any
    matched_term: int
    matched_on: tuple[str, ...]
    step_id: str | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "batch_id": self.batch_id,
            "tt": self.tt,
            "op_seq": self.op_seq,
            "arm": self.arm,
            "class": self.cls,
            "kind": self.kind,
            "identity": self.identity,
            "vt": self.vt,
            "matched_term": self.matched_term,
            "matched_on": list(self.matched_on),
        }
        if self.step_id is not None:
            out["step_id"] = self.step_id
        return out


def _witness_vt(fp: OpFootprint) -> Any:
    """§13.6 renders a single interval flat — `[45, 48]`, `[0, OPEN_END]` —
    and `"*"` for the carve arm."""
    if fp.vt is TOP:
        return TOP_JSON
    if len(fp.vt) == 1:
        return [fp.vt[0][0], fp.vt[0][1]]
    return [list(iv) for iv in fp.vt]


# ---------------------------------------------------------------------------
# the verdict (D13.24, D13.25)
# ---------------------------------------------------------------------------

State = Literal["fresh", "possibly-stale", "undecidable"]


@dataclass(frozen=True, slots=True)
class Verdict:
    """`FRESH | POSSIBLY_STALE(witnesses) | UNDECIDABLE(reason)`.

    **Ask `.actionable_fresh`, not `state == …`.** D13.25 says every consumer
    treats `UNDECIDABLE` as `POSSIBLY_STALE`; a caller who compares against an
    enum has to remember that, and the one who forgets writes `verdict.state
    != "possibly-stale"` and ships false freshness. There is exactly one
    question a caller should be asking and this type answers only that one
    affirmatively.
    """

    state: State
    witnesses: tuple[Witness, ...] = ()
    total: int = 0
    reason: str | None = None
    #: Widenings taken on the way to this verdict — a scan that started at 0
    #: because a `tt_q` was unverified, or because the cursor invariant did not
    #: hold. The verdict is sound either way; the harness reports the cell
    #: separately so a degraded `FRESH` is never counted as a narrow one.
    degraded: tuple[str, ...] = ()
    #: **E-10's mandatory receipt.** `None` unless step 8a exempted at least
    #: one batch, in which case `{"basis": α, "batches": n, "tt_range": [lo,
    #: hi], "theorem": "T1"}` — an exemption that does not say what it
    #: exempted and on what basis is indistinguishable from the bug (FF-4) it
    #: is distinguished from.
    exempt: dict[str, Any] | None = None

    @property
    def actionable_fresh(self) -> bool:
        """The **only** affirmative question. True iff nothing in the scanned
        suffix could have changed this result."""
        return self.state == "fresh"

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"verdict": self.state}
        if self.reason is not None:
            out["reason"] = self.reason
        if self.state == "possibly-stale":
            out["witnesses"] = [w.to_json() for w in self.witnesses]
            out["total"] = self.total
        if self.degraded:
            out["degraded"] = list(self.degraded)
        if self.exempt is not None:
            out["exempt"] = self.exempt
        return out


def FRESH(degraded: tuple[str, ...] = (), exempt: dict[str, Any] | None = None) -> Verdict:
    return Verdict("fresh", degraded=degraded, exempt=exempt)


def POSSIBLY_STALE(witnesses: Sequence[Witness], total: int,
                   degraded: tuple[str, ...] = (),
                   exempt: dict[str, Any] | None = None) -> Verdict:
    return Verdict("possibly-stale", tuple(witnesses), total, degraded=degraded, exempt=exempt)


def UNDECIDABLE(reason: str, degraded: tuple[str, ...] = ()) -> Verdict:
    if reason not in REASONS:
        raise InvalidArgError(f"unknown refusal reason: {reason!r}", allowed=list(REASONS))
    return Verdict("undecidable", reason=reason, degraded=degraded)


# ---------------------------------------------------------------------------
# the chain cache (§3.9)
# ---------------------------------------------------------------------------

def _stat(path: Any) -> os.stat_result:
    """The cache's one stat call, behind a module-level name so a test can
    freeze it and reproduce a same-tick rewrite on any host."""
    return os.stat(path)


def _fingerprint(path: Any, size: int) -> str:
    """`sha256` over exactly the first `size` bytes of `path`."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        remaining = size
        while remaining > 0:
            chunk = fh.read(min(remaining, 1 << 20))
            if not chunk:
                break
            remaining -= len(chunk)
            h.update(chunk)
    return h.hexdigest()


#: How many trailing bytes of the log a check inside a burst re-reads. A
#: correction batch record is a few hundred bytes, so this covers the last
#: record whole on the logs this cache serves; it is a cheap tripwire, not
#: the guarantee (see `ChainCache.burst`).
_TAIL_BYTES = 4096


def _tail(path: Any, size: int) -> bytes:
    """The last `min(size, _TAIL_BYTES)` bytes of the first `size` bytes."""
    with open(path, "rb") as fh:
        start = max(0, size - _TAIL_BYTES)
        fh.seek(start)
        return fh.read(size - start)


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    walk: "LogWalk"
    fingerprint: str


@dataclass(frozen=True, slots=True)
class _BurstState:
    """One log state a check earlier in the current burst validated in full:
    its stat key, its last `_TAIL_BYTES` bytes, the entry the full fingerprint
    vouched for, and — once a later check in the burst has asked for it while
    the state still held — the store identity."""

    key: tuple[str, int, int, int, int]
    tail: bytes
    entry: _CacheEntry
    identity: str | None = None


class ChainCache:
    """A memo over `{(path, size, mtime_ns, ino, ctime_ns): (walk, sha256)}`.

    `chain_of_prefix(offset)` iterates from byte 0, and D13.24 step 6 requires
    it for **every** checkpoint, so a naive checker is O(whole log) per check
    and a harness's thousands of checks each rescan the whole file. One walk
    yields every prefix chain, so the cache is free once the walk happens.

    **It is an implementation convenience and is off by default.** D13.26's
    cost claim is about the mechanism, not about a memo, so `check` walks
    unless a cache is handed to it — which is what lets the report state the
    number with and without rather than quietly reporting the cached one.

    Keyed on size, mtime, **inode and ctime** — and, on every stat-key hit, the
    entry is served only after a `sha256` over the first `size` bytes of the
    file matches the digest taken when the entry was populated (D-162,
    2026-09-19). The walk is also the tamper-evidence check (D13.18), and a
    cache that answered from a stale entry would be answering the one question
    it exists to verify — but a stat key alone cannot rule that out. On the
    `xzws` baseline host (Linux 5.4.0-216, `HZ=250`) `st_mtime_ns` and
    `st_ctime_ns` advance only on a 4 ms jiffy, so a same-size in-place rewrite
    that completes inside one tick leaves every stat field byte-identical and
    aliases the pre-rewrite entry; on tmpfs (`/dev/shm`) that is the common
    case, not the rare one.

    The digest must cover the **whole cached prefix**: a cheaper "last block"
    fingerprint would not catch the memo's own failing case, a same-length
    rewrite in the *middle* of the checkpointed prefix. A plain digest is one
    sequential read with no JSON parsing and no chain arithmetic, so a hit is
    still much cheaper than the walk, while a stale entry can no longer be
    served whatever the host's timestamp resolution.

    **Bursts share that digest** (failure ledger `check-fingerprint-per-check`,
    pre-registered as P-ENG2). Outside a burst, nothing above changes. Inside
    `with cache.burst():` the first check of each log state takes the path
    above — a hit re-digests the whole prefix, a miss re-walks — and records
    the state's stat key and last `_TAIL_BYTES` bytes; every later check in
    the same burst that finds the same stat key *and* the same tail bytes is
    served that entry (and, from the second check on, the store identity)
    without re-reading the prefix. Any change of size, mtime, inode, ctime or
    tail sends the check back to the full path. See `burst` for what the
    caller vouches for and what that does and does not keep of D-162.

    Behaviour is otherwise unchanged: the cache stays opt-in, verdicts with and
    without it are identical, and the counters keep their meaning — a
    fingerprint mismatch is a **miss** (the log is re-walked and the entry
    replaced), and a check served from a burst's shared state is a **hit**.
    `fingerprints` counts whole-prefix digests taken, so the sharing is
    observable. A caller auditing an adversarial log should still pass `None`.
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[str, int, int, int, int], _CacheEntry] = {}
        self.hits = 0
        self.misses = 0
        #: Whole-prefix `sha256` digests taken (the D-162 cost), miss or hit.
        self.fingerprints = 0
        self._burst_depth = 0
        self._burst: dict[str, _BurstState] = {}

    @staticmethod
    def _key(log: EventLog) -> tuple[str, int, int, int, int]:
        st = _stat(log.path)
        return (str(log.path), st.st_size, st.st_mtime_ns, st.st_ino, st.st_ctime_ns)

    @contextmanager
    def burst(self) -> Iterator["ChainCache"]:
        """A block of checks that share one validation per log state.

        **What the caller vouches for:** nothing rewrites the log in place
        while the block runs. The storm harness is the shape this is for: it
        is the store's single writer (it holds `writer.lock`), it appends
        exactly one correction batch, and only then runs a block of hundreds
        of checks against the log it just wrote, appending nothing until the
        block ends.

        **What D-162 keeps.** Outside every burst, every hit still re-digests
        the whole prefix, so `test_a_same_tick_same_size_tamper_is_caught_
        with_the_cache_in_hand` is unchanged. Inside a burst, the first check
        of each log state still re-digests the whole prefix, so an in-place
        rewrite made *before* the block — the D-162 case, which a stat key
        cannot see — is caught there, as before; and any append, truncation,
        or rewrite that moves a stat field or touches the last `_TAIL_BYTES`
        bytes is caught at the next check in the block. What a burst gives up
        is exactly one case: a same-size, same-tick, in-place rewrite *of the
        middle of the log during the block*, which the caller has said does
        not happen. The burst ends (and its shared state is forgotten) when
        the outermost `with` exits; bursts nest.
        """
        self._burst_depth += 1
        try:
            yield self
        finally:
            self._burst_depth -= 1
            if self._burst_depth == 0:
                self._burst.clear()

    def _burst_state(self, log: EventLog) -> _BurstState | None:
        """The state an earlier check in this burst validated, if the log
        still shows the same stat key and the same tail bytes — else `None`,
        and the caller takes the full path."""
        if self._burst_depth == 0:
            return None
        state = self._burst.get(str(log.path))
        if state is None:
            return None
        key = self._key(log)
        if key != state.key or _tail(log.path, key[1]) != state.tail:
            return None
        return state

    def walk(self, log: EventLog) -> "LogWalk":
        state = self._burst_state(log)
        if state is not None:
            self.hits += 1
            return state.entry.walk
        key = self._key(log)
        size = key[1]
        entry: _CacheEntry | None = None
        cached = self._entries.get(key)
        if cached is not None:
            self.fingerprints += 1
            if _fingerprint(log.path, size) == cached.fingerprint:
                self.hits += 1
                entry = cached
        if entry is None:
            self.misses += 1
            walked = _walk(log)
            self.fingerprints += 1
            entry = _CacheEntry(walked, _fingerprint(log.path, size))
            self._entries[key] = entry
        if self._burst_depth:
            self._burst[key[0]] = _BurstState(key, _tail(log.path, size), entry)
        return entry.walk

    def store_identity(self, log: EventLog) -> str:
        """D13.24 step 3's identity, shared across a burst the same way.

        Outside a burst, or before a check in this burst has validated the
        current log state, this is `_store_identity_of(log)` — the per-log
        memo, revalidated against the genesis bytes on every call. Once a
        state is validated (its whole prefix digested, which covers the
        genesis record) and still holds, the identity computed by the first
        check that asked while it held is served to the rest of the burst."""
        state = self._burst_state(log)
        if state is not None and state.identity is not None:
            return state.identity
        identity = _store_identity_of(log)
        if state is not None:
            self._burst[state.key[0]] = replace(state, identity=identity)
        return identity

    def prefix_chains(self, log: EventLog) -> dict[int, str]:
        return self.walk(log).chains


@dataclass(frozen=True, slots=True)
class LogWalk:
    """What one pass over the log yields: every record boundary's rolling
    chain, every record's `(tt, start_offset)`, and the identities whose
    believed versions overlap at the end of the log (step 7b).

    The chains and starts come from the same pass **on purpose** (E-2).
    D13.24's step 6 requires `chain_of_prefix` for every checkpoint and that
    iterates from byte 0 regardless, so step 5's cursor invariant — *is
    `start` at or before the first batch with `tt > tt_q`?* — is answered from
    what the walk already collected instead of by a second scan. E-2's own
    words: *"Step 5's cursor invariant is verified inside step 6's walk, so it
    costs nothing extra and should not appear as a separate line."*

    The overlap set is keyed by what that pass computed: the chain at the
    log's end is a digest of every record, so the interval replay behind it
    runs once per log state (`_overlaps_at`) — from byte 0 the first time,
    over only the appended records when the log extends a state already
    replayed — and a `ChainCache` keeps the result with the chains.
    """

    chains: dict[int, str]
    starts: tuple[tuple[int, int], ...]      # (tt, record start offset), in log order
    overlaps: tuple["KnownOverlap", ...] = ()

    def first_start_after(self, tt_q: int) -> int | None:
        """The start offset of the first batch in the `tt`-suffix, or `None`
        when the suffix is empty."""
        for tt, start in self.starts:
            if tt > tt_q:
                return start
        return None


def _walk(log: EventLog) -> LogWalk:
    """One pass. `{0: SEED_CHAIN}` for an empty log, which is what
    `chain_of_prefix(0)` returns."""
    chains = {0: SEED_CHAIN}
    chain = SEED_CHAIN
    starts: list[tuple[int, int]] = []
    last = 0
    for batch, end, raw in log.batches_from(0):
        chain = extend_chain(chain, raw)
        chains[end] = chain
        starts.append((int(batch["tt"]), end - len(raw)))
        last = end
    return LogWalk(chains, tuple(starts), _overlaps_at(log, chains, last))


# ---------------------------------------------------------------------------
# the known-overlap precondition (step 7b)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class KnownOverlap:
    """One identity whose believed versions overlap in valid time at the end
    of the log — what `tgms check --full` reports as
    `row/believed-versions-overlap`, derived from the log alone.

    `footprint` is how step 7b asks whether a scope could have read it: the
    identity, its relation type, `props = "*"` (two versions disagree about
    every property, existence and event count over the overlap), and `vt` =
    the hull of **every** believed version of the identity, closed at the
    right like every other footprint (D13.21) — wider than the overlapping
    region itself, which is the safe direction. `kind` is filled in only
    because an `OpFootprint` needs one: step 7b does not test the kinds
    conjunct (see `_touches`)."""

    footprint: OpFootprint
    intervals: tuple[tuple[int, int], ...]

    def to_json(self) -> dict[str, Any]:
        return {"entity_kind": self.footprint.entity_kind,
                "identity": self.footprint.identity.to_json(),
                "intervals": [list(iv) for iv in self.intervals]}


class _Refused(Exception):
    """The applier raises on this op, so its whole batch rolls back (the
    store skips it on the live path and on replay alike, `Store._write`)."""


def _overlapping(a_s: Any, a_e: Any, b_s: Any, b_e: Any) -> bool:
    return a_s < b_e and b_s < a_e


def _remainder(vs: Any, ve: Any, cs: Any, ce: Any) -> list[tuple[Any, Any]]:
    """`tgms.storage.base._remainder`, restated: the parts of `[vs, ve)` not
    covered by `[cs, ce)`."""
    out = []
    if vs < cs:
        out.append((vs, min(ve, cs)))
    if ce < ve:
        out.append((max(vs, ce), ve))
    return out


def _interval(start: Any, end: Any) -> tuple[Any, Any]:
    """`base._interval`'s validation (`Interval`'s `start < end`), refusing
    where it raises."""
    try:
        Interval(start, end)
    except (ValueError, TypeError):
        raise _Refused() from None
    return start, end


class _Believed:
    """The log replayed over **intervals only** — which `(vt_s, vt_e)` each
    identity believes at the end of the log, and nothing else (no props, no
    `tt`, no version ids). It restates the five appliers of
    `tgms/storage/base.py` (`StorageAdapter`, which every backend inherits
    unchanged) exactly as far as they decide which intervals stay believed:

    - `assert_node` / `assert_edge`: every believed version overlapping the
      asserted interval is replaced by its remainders, then the new interval
      is added;
    - `correct`: the same, refusing when nothing overlaps (and, for a node,
      when the hits disagree on their label, D-140);
    - `retract`: every version valid at `t` is cut to `[vt_s, t)`, refusing
      when none is;
    - `ingest_events`: every event interval is **added, superseding nothing**
      — the one way two believed versions of one identity come to overlap;
      explicit `nodes` refuse on a duplicate or on a uid already believed, and
      endpoints with no believed version get `[first seen, OPEN_END)`.

    A batch with a refused op is rolled back whole, as the store rolls it
    back. Edge identities are keyed on the four logged fields `edge_eid`
    hashes, node identities on the uid, so nothing here hashes per event.
    The node table carries labels because `correct` refuses on them; node
    overlaps are unreachable through these appliers and are tracked anyway,
    so a log that somehow contains one is still caught.

    One known divergence, in the safe direction: a batch that writes one
    identity twice at one `vt_s` gives both rows one version id, and the
    native engine's close/retire-by-id then stops believing both where this
    replay stops believing only the hit — so this replay can believe more
    than the store, never less, and can only over-report an overlap.
    """

    __slots__ = ("edges", "nodes", "_multi_e", "_multi_n", "_undo")

    def __init__(self) -> None:
        #: edge key `(src, dst, rel_type, disc)` -> believed `[(vt_s, vt_e)]`
        self.edges: dict[tuple[Any, Any, Any, Any], list[tuple[Any, Any]]] = {}
        #: uid -> believed `[(vt_s, vt_e, label)]`
        self.nodes: dict[Any, list[tuple[Any, Any, Any]]] = {}
        #: identities that have ever held two or more believed versions — the
        #: only ones the end-of-log overlap test has to look at (a superset is
        #: harmless: `overlaps` re-tests each)
        self._multi_e: set[tuple[Any, Any, Any, Any]] = set()
        self._multi_n: set[Any] = set()
        #: the current batch's undo actions, applied in reverse on a refusal
        self._undo: list[tuple[Any, ...]] = []

    # -- replaying ------------------------------------------------------------

    def replay(self, log: EventLog, start: int, chain: str, end: int,
               chains: dict[int, str]) -> None:
        """Apply every record in `[start, end)` — the walk's own records:
        each one's rolling chain must equal the one the walk computed, or the
        log changed between the walk and this read, and the check refuses
        (`StateError`, which `check` reports as `log-unreadable`)."""
        at = start
        if at < end:
            for batch, b_end, raw in log.batches_from(start):
                chain = extend_chain(chain, raw)
                if b_end > end or chains.get(b_end) != chain:
                    break
                self.apply_batch(batch)
                at = b_end
                if at == end:
                    break
        if at != end:
            raise StateError("the event log changed while it was being checked",
                             offset=at, expected=end)

    def apply_batch(self, batch: dict[str, Any]) -> None:
        undo = self._undo = []
        try:
            for op in batch.get("ops", ()):
                self._apply_op(op)
        except (_Refused, KeyError, TypeError, ValueError, AttributeError):
            # `_Refused` is the applier's own refusal; the rest are an op the
            # applier cannot have applied either (a missing or mistyped field
            # raises there just the same). Roll the whole batch back.
            for action in reversed(undo):
                if action[0] == "ingest":
                    for key in reversed(action[2]):
                        self.edges[key].pop()
                    for key in action[1]:
                        self.edges.pop(key, None)
                else:
                    table = self.edges if action[0] == "edge" else self.nodes
                    if action[2] is None:
                        table.pop(action[1], None)
                    else:
                        table[action[1]] = action[2]
        self._undo = []

    def _set_edge(self, key: Any, versions: list[tuple[Any, Any]]) -> None:
        """Replace `key`'s believed list (never mutating the old one, so the
        undo action can hold it by reference)."""
        self._undo.append(("edge", key, self.edges.get(key)))
        if versions:
            self.edges[key] = versions
            if len(versions) > 1:
                self._multi_e.add(key)
        else:
            self.edges.pop(key, None)

    def _set_node(self, uid: Any, versions: list[tuple[Any, Any, Any]]) -> None:
        self._undo.append(("node", uid, self.nodes.get(uid)))
        if versions:
            self.nodes[uid] = versions
            if len(versions) > 1:
                self._multi_n.add(uid)
        else:
            self.nodes.pop(uid, None)

    # -- the five appliers, over intervals -----------------------------------

    def _apply_op(self, op: dict[str, Any]) -> None:
        kind = op["op"]
        if kind == "ingest_events":
            self._ingest_events(op)
        elif kind == "assert_edge":
            key = (op["src"], op["dst"], op["rel_type"], op.get("disc", ""))
            vt = _interval(op["vt_s"], op.get("vt_e", OPEN_END))
            self._set_edge(key, self._replace(self.edges.get(key, ()), vt) + [vt])
        elif kind == "assert_node":
            uid, label = op["uid"], op["label"]
            vt = _interval(op["vt_s"], op.get("vt_e", OPEN_END))
            self._set_node(uid, self._replace(self.nodes.get(uid, ()), vt)
                           + [(vt[0], vt[1], label)])
        elif kind == "retract":
            ref, t = op["ref"], op["t"]
            if ref["kind"] == "node":
                uid = ref["uid"]
                versions = self.nodes.get(uid, ())
                hits = [v for v in versions if v[0] <= t < v[1]]
                if not hits:
                    raise _Refused()
                self._set_node(uid, [v for v in versions if not v[0] <= t < v[1]]
                               + [(v[0], t, v[2]) for v in hits if v[0] < t])
            else:
                key = self._edge_ref(ref)
                versions_e = self.edges.get(key, ())
                hits_e = [v for v in versions_e if v[0] <= t < v[1]]
                if not hits_e:
                    raise _Refused()
                self._set_edge(key, [v for v in versions_e if not v[0] <= t < v[1]]
                               + [(v[0], t) for v in hits_e if v[0] < t])
        elif kind == "correct":
            ref = op["ref"]
            op["props"]                       # `base._correct` reads it first
            vt = _interval(op["vt_s"], op.get("vt_e", OPEN_END))
            if ref["kind"] == "node":
                uid = ref["uid"]
                versions = self.nodes.get(uid, ())
                hits = [v for v in versions if _overlapping(v[0], v[1], *vt)]
                if not hits or len({v[2] for v in hits}) > 1:
                    raise _Refused()
                self._set_node(uid, self._replace(versions, vt)
                               + [(vt[0], vt[1], hits[0][2])])
            else:
                key = self._edge_ref(ref)
                versions_e = self.edges.get(key, ())
                if not any(_overlapping(v[0], v[1], *vt) for v in versions_e):
                    raise _Refused()
                self._set_edge(key, self._replace(versions_e, vt) + [vt])
        else:
            raise _Refused()

    @staticmethod
    def _edge_ref(ref: dict[str, Any]) -> tuple[Any, Any, Any, Any]:
        if ref["kind"] != "edge":
            raise _Refused()
        return (ref["src"], ref["dst"], ref["rel_type"], ref.get("disc", ""))

    @staticmethod
    def _replace(versions: Any, vt: tuple[Any, Any]) -> list[Any]:
        """Every version not overlapping `vt`, plus the remainders of the
        ones that do — what survives a supersede at `vt` (labels, where
        present, ride along on the remainders)."""
        out: list[Any] = []
        cs, ce = vt
        for v in versions:
            if not _overlapping(v[0], v[1], cs, ce):
                out.append(v)
                continue
            for fs, fe in _remainder(v[0], v[1], cs, ce):
                out.append((fs, fe) + tuple(v[2:]))
        return out

    def _ingest_events(self, op: dict[str, Any]) -> None:
        """Everything that can refuse runs before anything is written, as in
        `base._ingest_events`; the event loop is the hot path of a bulk load
        and is written for it."""
        events = op.get("events") or []
        base = op.get("offset", 0)
        rows: list[tuple[tuple[Any, Any, Any, Any], tuple[Any, Any]]] = []
        first_seen: dict[Any, Any] = {}
        seen_get = first_seen.get
        for i, ev in enumerate(events):
            src, dst, vt_s = ev["src"], ev["dst"], ev["vt_s"]
            disc = ev["disc"] if "disc" in ev else f"#{base + i}"
            rows.append(((src, dst, ev["rel_type"], disc), (vt_s, ev.get("vt_e") or vt_s + 1)))
            v = seen_get(src)
            if v is None or vt_s < v:
                first_seen[src] = vt_s
            v = seen_get(dst)
            if v is None or vt_s < v:
                first_seen[dst] = vt_s
        explicit: list[tuple[Any, tuple[Any, Any, Any]]] = []
        nodes = op.get("nodes")
        if nodes:
            seen: set[Any] = set()
            for rec in nodes:
                if rec["uid"] in seen:
                    raise _Refused()
                seen.add(rec["uid"])
            if any(self.nodes.get(uid) for uid in seen):
                raise _Refused()
            for rec in nodes:
                vt = _interval(rec["vt_s"], rec.get("vt_e", OPEN_END))
                explicit.append((rec["uid"], (vt[0], vt[1], rec["label"])))
        label = op.get("node_label", "Node")
        explicit_uids = {uid for uid, _v in explicit}
        for uid, version in explicit:
            self._set_node(uid, [version])
        believed_nodes = self.nodes
        for u, vt_s in first_seen.items():
            if u in explicit_uids or believed_nodes.get(u):
                continue
            self._set_node(u, [(vt_s, OPEN_END, label)])
        # filed before the loop and filled as it goes, so a field that fails
        # mid-loop (an unhashable one) still rolls back exactly what landed
        created: list[Any] = []
        appended: list[Any] = []
        self._undo.append(("ingest", created, appended))
        edges, multi = self.edges, self._multi_e
        edges_get = edges.get
        for key, vt in rows:
            prior = edges_get(key)
            if prior is None:
                edges[key] = [vt]
                created.append(key)
            else:
                prior.append(vt)
                appended.append(key)
                multi.add(key)

    # -- the result -----------------------------------------------------------

    def overlaps(self) -> tuple[KnownOverlap, ...]:
        """`integrity.rs::overlaps`' rule over what is believed now: sorted
        by `vt_s`, some interval ends after the next one starts."""
        out: list[KnownOverlap] = []
        for key in self._multi_e:
            ivs = self.edges.get(key)
            if ivs is None or not _has_overlap(ivs):
                continue
            src, dst, rel_type, disc = (str(x) for x in key)
            ident = Identity(eid=(edge_eid(src, dst, rel_type, disc),), src=(src,),
                             dst=(dst,), rel_type=(rel_type,), disc=(disc,))
            out.append(_known(ident, "edge", (rel_type,), sorted(ivs)))
        for uid in self._multi_n:
            nvs = self.nodes.get(uid)
            if nvs is None or not _has_overlap(nvs):
                continue
            out.append(_known(Identity(uid=(str(uid),)), "node", TOP,
                              sorted((v[0], v[1]) for v in nvs)))
        out.sort(key=lambda k: (k.footprint.entity_kind,
                                repr(k.footprint.identity.to_json())))
        return tuple(out)


@dataclass(slots=True)
class _Replay:
    """One replayed log state: where it ends, the walk's chain there, the
    replay itself, and its overlaps."""

    end: int
    chain: str
    believed: _Believed
    overlaps: tuple[KnownOverlap, ...]


#: The last few replayed log states, content-addressed by `(end, chain)` —
#: the rolling chain the walk computes anyway is a digest of every record up
#: to `end`, so a state is reused only for a log whose records are those
#: bytes, whichever `EventLog` object or path reads them. A walk whose log
#: extends a remembered state takes it and replays only the new records
#: (ownership moves: the state is mutated forward and re-filed under its new
#: end). Process memory only; it cannot change a verdict, only skip
#: replaying records whose replay is already known — the posture of
#: `_IDENTITY_MEMOS`, and like it, not opt-in.
_REPLAYS: list[_Replay] = []
_REPLAYS_KEPT = 2
_REPLAYS_LOCK = threading.Lock()


def _overlaps_at(log: EventLog, chains: dict[int, str], end: int) -> tuple[KnownOverlap, ...]:
    """The known overlaps of the log state the walk just read (`chains`, up
    to `end`), replaying only what no remembered state already covers."""
    if end == 0:
        return ()
    with _REPLAYS_LOCK:
        best: _Replay | None = None
        for r in _REPLAYS:
            if chains.get(r.end) == r.chain and (best is None or r.end > best.end):
                best = r
        if best is not None and best.end == end:
            return best.overlaps
        if best is not None:
            _REPLAYS.remove(best)
    if best is None:
        believed, start, chain = _Believed(), 0, SEED_CHAIN
    else:
        believed, start, chain = best.believed, best.end, best.chain
    believed.replay(log, start, chain, end, chains)
    overlaps = believed.overlaps()
    with _REPLAYS_LOCK:
        _REPLAYS.append(_Replay(end, chains[end], believed, overlaps))
        del _REPLAYS[:-_REPLAYS_KEPT]
    return overlaps


def _has_overlap(versions: Sequence[tuple[Any, ...]]) -> bool:
    ivs = sorted((v[0], v[1]) for v in versions)
    return any(a[1] > b[0] for a, b in zip(ivs, ivs[1:]))


def _known(identity: Identity, entity_kind: Any, rel_type: Any,
           intervals: list[tuple[Any, Any]]) -> KnownOverlap:
    lo = min(s for s, _e in intervals)
    hi = max(e for _s, e in intervals)
    vt = ((int(lo), min(int(hi) + 1, OPEN_END)),) if int(lo) < int(hi) else TOP
    return KnownOverlap(
        footprint=OpFootprint(seq=0, arm="value", kind="ingest_events", cls="A",
                              entity_kind=entity_kind, identity=identity,
                              rel_type=rel_type, vt=vt, props=TOP),
        intervals=tuple((s, e) for s, e in intervals))


def _touches(term: ScopeTerm, fp: OpFootprint) -> bool:
    """Could a read described by `term` have read the identity `fp` names?

    `intersects` minus its kinds conjunct. `kinds` says which *later writes*
    could change a result; a known overlap is not a later write but a state
    the read may already have seen, so no kind set can exclude it. The other
    four conjuncts — entity kind and identity (through the target arms),
    relation type, valid time, properties — are `intersects`' own. A target
    arm that would `REFUSE` counts as touching: the safe direction."""
    if fp.entity_kind == "edge" and not meets(term.rel_types, fp.rel_type):
        return False
    if not vt_overlaps(term.vt, fp.vt):
        return False
    if not meets(term.props, fp.props):
        return False
    m, _arm = targets_match(term.targets, fp)
    return m is not Match.MISS


def known_overlap(scope: DependencyScope, overlaps: Sequence[KnownOverlap]) -> bool:
    """Step 7b's test: does any term of `scope` touch any known overlap?"""
    return any(_touches(term, k.footprint) for k in overlaps for term in scope.terms)


#: One `StoreIdentityMemo` per live `EventLog` object (weakly keyed: it dies
#: with the log object, never outlives the process, never touches disk).
_IDENTITY_MEMOS: "weakref.WeakKeyDictionary[Any, StoreIdentityMemo]" = \
    weakref.WeakKeyDictionary()


def _store_identity_of(log: EventLog) -> str:
    """D13.24 step 3's `store_identity(log.header(), log.first_batch())`,
    through `log`'s own memo (`StoreIdentityMemo`: byte-revalidated, so the
    value and every failure are those of the uncached expression). A log
    object that cannot be weakly referenced takes the uncached expression."""
    try:
        memo = _IDENTITY_MEMOS.get(log)
        if memo is None:
            memo = _IDENTITY_MEMOS.setdefault(log, StoreIdentityMemo())
    except TypeError:
        return store_identity(log.header(), log.first_batch())
    return memo.identity(log)


# ---------------------------------------------------------------------------
# check (D13.24)
# ---------------------------------------------------------------------------

def _unknown_enum(scope: DependencyScope) -> bool:
    """Step 2. `ScopeTerm.__post_init__` already refuses unknown kinds, roles,
    `vt_mode`s and pseudo-props **at construction**, so this path is reachable
    only for a scope deserialized from a *future* version — which step 1's
    version gate catches first. It is implemented anyway, because
    "unreachable" is exactly what FF-8 was."""
    for term in scope.terms:
        if term.kinds is not TOP and any(k not in KINDS for k in term.kinds):
            return True
        if term.vt_mode not in VT_MODES:
            return True
        if term.props is not TOP and any(
                p.startswith("@") and p not in PSEUDO_PROPS for p in term.props):
            return True
        targets = term.targets
        if isinstance(targets, Targets) and isinstance(targets.incident, Incident):
            if targets.incident.role not in INCIDENT_ROLES:
                return True
    return False


def _parse(scope: DependencyScope | dict[str, Any]) -> DependencyScope | str:
    """A scope in hand, or the reason it cannot be one. A dict missing `tt_q`
    is D13.24 step 4's `no-tt_q` — which is only reachable from the wire, since
    the dataclass makes the field mandatory."""
    if isinstance(scope, DependencyScope):
        return scope
    if not isinstance(scope, dict):
        return "scope-version"
    if scope.get("tt_q") is None:
        return "no-tt_q"
    try:
        return DependencyScope.from_json(scope)
    except InvalidArgError:
        return "scope-version"
    except (KeyError, TypeError, ValueError):
        return "scope-version"


def check(scope: DependencyScope | dict[str, Any], log: EventLog,
          tt_now: int = OPEN_END, *, step_id: str | None = None,
          term_steps: Sequence[str | None] | None = None,
          chain_cache: ChainCache | None = None,
          witness_cap: int = WITNESS_CAP) -> Verdict:
    """D13.24's nine steps.

    `tt_now` **defaults to `OPEN_END` — scan the whole suffix** (coordinator
    ruling, D-M4a). The rounding direction here is the *opposite* of `tt_q`'s:
    `tt_q` rounds **down** because a `tt_q` above what the read saw skips the
    suffix that would invalidate it; `tt_now` rounds **up** because a `tt_now`
    below a batch that is already in the log excludes that batch from the scan
    while every recomputing reader can see it. The log is fsynced *before*
    apply, so the log always leads the frontier — which makes "pass the applied
    frontier as `tt_now`" the false-fresh direction, and it is never the
    default. A caller passing a smaller `tt_now` is asking a narrower question
    ("as of last Tuesday") and owns it.

    `term_steps` is the merged-plan-scope path: a parallel sequence naming, per
    term, the step whose scope contributed it. `⊎` is concatenation (D13.8), so
    that provenance survives the union and a witness against a merged scope can
    still say which step it hit — which is what §13.6's worked example reports,
    with `matched_term` indexing the merged list and `step_id` naming the
    origin. With none, every witness takes `step_id`.

    A scope whose `tt_q_verified` is false **widens rather than refusing**
    (coordinator ruling, D-M4b): `tt_q := 0` and the scan starts at byte 0, so
    nothing can be skipped and the verdict can still honestly be `FRESH`.
    Refusing would be equally sound and strictly less useful — `UNDECIDABLE` is
    read as `POSSIBLY_STALE` by every consumer, so it can only ever say "don't
    know". The widening is recorded as `degraded: ["tt_q-unverified"]`.

    **Step 8a (§15 2026-08-27 entry E-10) exempts a batch, per-item, when the
    scope carries `as_of_tt` and `batch.tt > as_of_tt`.** `as_of_tt` is
    additive and opt-in (absent means no exemption — every scope written
    before E-10 gets byte-identical verdicts); `tt_q_verified: false`
    suppresses it entirely, whatever `as_of_tt` says, because an unverified
    basis is exactly the case where `as_of_tt`'s relationship to the log is
    unknown. Nothing is skipped: every batch in the suffix is still
    enumerated and individually adjudicated, and steps 1-7 are unaffected.
    The verdict carries `exempt: {basis, batches, tt_range, theorem}` whenever
    at least one batch was exempted — the receipt is mandatory, never a silent
    skip.

    **Step 7b (P-ENG2) refuses with `known-overlap`** when a term could have
    read an identity whose believed versions overlap at the end of the log —
    see `KnownOverlap` and `_touches`. On a log with no such identity it
    changes nothing, which is every log the write path builds without an
    explicit-`disc` collision in `ingest_events`.
    """
    parsed = _parse(scope)
    if isinstance(parsed, str):
        return UNDECIDABLE(parsed)
    scope = parsed
    degraded: list[str] = []

    # 1 — the version gate (D13.9). First, because a future version's terms
    #     cannot be interpreted at all, including their enums.
    if scope.version != 1:
        return UNDECIDABLE("scope-version")

    # 2 — an unrecognized enum anywhere in any term (D13.23a)
    if _unknown_enum(scope):
        return UNDECIDABLE("unknown-enum")

    # 3 — the store identity. UNANCHORED is *always* a mismatch: an
    #     adapter-only read has no log behind it, so there is nothing this log
    #     could be the continuation of.
    #     The identity is served from a per-`EventLog`-object memo
    #     (`_store_identity_of`) revalidated against the log's exact genesis
    #     bytes on every call, so it is the same value — and the same
    #     exception — the uncached expression gives; unlike `ChainCache` it
    #     is not opt-in, because it cannot change a verdict, only skip
    #     re-parsing a multi-megabyte genesis record (failure ledger
    #     `registry-store-identity-recomputed`).
    #     Inside a `ChainCache.burst`, a state an earlier check validated in
    #     full serves the identity too (`ChainCache.store_identity`).
    try:
        identity = (chain_cache.store_identity(log) if chain_cache is not None
                    else _store_identity_of(log))
    except (StateError, OSError, ValueError):
        return UNDECIDABLE("log-unreadable")
    if scope.store == UNANCHORED or scope.store != identity:
        return UNDECIDABLE("store-mismatch")

    # 4 — tt_q. Mandatory on the dataclass; absent only from the wire, handled
    #     in `_parse`.
    tt_q = scope.tt_q

    # 4b — the unverified widening (D-M4b)
    if not scope.tt_q_verified:
        tt_q = 0
        degraded.append("tt_q-unverified")

    # 5 + 6 — the cursor invariant and the chain verification, in ONE walk.
    #     `chain_of_prefix` iterates from byte 0 and step 6 needs it for every
    #     checkpoint, so the walk happens regardless; verifying step 5 inside
    #     it costs nothing extra, which is §8.1's mitigation for D13.26's cost
    #     claim being false as implemented.
    try:
        walked = (chain_cache.walk(log) if chain_cache is not None else _walk(log))
    except (StateError, OSError, ValueError):
        return UNDECIDABLE("log-unreadable")

    for cp in scope.checkpoints:
        actual = walked.chains.get(cp.offset)
        if actual is None:
            # `chain_of_prefix` raises rather than returning a mismatch when
            # the offset is not a record boundary — a third outcome, and it
            # refuses like the other two.
            return UNDECIDABLE("log-unreadable", tuple(degraded))
        if actual != cp.chain:
            return UNDECIDABLE("log-rewritten", tuple(degraded))

    start = scope.min_offset
    first_suffix = walked.first_start_after(tt_q)
    if first_suffix is not None and start > first_suffix:
        # D13.8a sanctions the widening in as many words: reset and continue.
        # Refusing would also be sound and would tell the caller nothing.
        # Batches lying in the tt-suffix but *before* the retained offset would
        # otherwise never be scanned, and the verdict would be `FRESH` on a
        # changed result.
        start = 0
        degraded.append("cursor-invariant")

    # 7 — the empty scope. Nothing can ever invalidate this result (D5.3).
    #     Checked *after* the integrity steps so that a ∅ scope over a
    #     rewritten log still refuses.
    if scope.is_empty:
        return FRESH(tuple(degraded))

    # 7b — the known overlap (P-ENG2, failure ledger
    #     `check-assumed-non-overlap-precondition`). Steps 8-9 and every
    #     scope derivation assume I1: no two believed versions of one
    #     identity overlap in valid time. The walk replayed the log over
    #     intervals and kept the identities for which that is false at its
    #     end (what `tgms check --full` reports as `believed-versions-overlap`);
    #     if a term could have read one of them, the answer rests on a
    #     precondition known to be false, and "don't know" is the only honest
    #     verdict. Checked after the integrity steps, so a rewritten log still
    #     says so, and after step 7, since an empty scope reads nothing. A
    #     `⊤` term touches every identity, so on a log with any overlap at
    #     all it lands here.
    if walked.overlaps and known_overlap(scope, walked.overlaps):
        return UNDECIDABLE("known-overlap", tuple(degraded))

    # 8 — the scan. BOTH arms of every op (D13.21a), each term separately.
    witnesses: list[Witness] = []
    seen: set[tuple[str | None, str, int]] = set()
    total = 0
    #: Step 8a's tally (E-10) — `None` unless at least one batch is exempted.
    exempt_count = 0
    exempt_lo: int | None = None
    exempt_hi: int | None = None
    #: The exemption is licensed only for a **verified** basis (D-153 point 3):
    #: an unverified `tt_q` is exactly the case where `as_of_tt`'s relationship
    #: to the log is unknown, and it must suppress the exemption entirely,
    #: whatever `as_of_tt` says.
    exemption_active = scope.as_of_tt is not None and scope.tt_q_verified
    try:
        for batch, _end, _raw in log.batches_from(start):
            tt = int(batch["tt"])
            if not (tt_q < tt <= tt_now):
                continue
            # 8a — the per-batch pinned exemption (E-10), BEFORE footprint
            # construction. A per-item test against this batch's own logged
            # `tt`, not a verdict computed from an unverified flag: T1's proof
            # restricted to one batch says `B` cannot change a result read at
            # `as_of_tt = α` when `B.tt > α`, so the batch is adjudicated —
            # exempted, not skipped-without-evidence — and enumeration
            # continues exactly as it does for every other batch.
            if exemption_active and tt > scope.as_of_tt:  # type: ignore[operator]
                exempt_count += 1
                exempt_lo = tt if exempt_lo is None else min(exempt_lo, tt)
                exempt_hi = tt if exempt_hi is None else max(exempt_hi, tt)
                continue
            try:
                fps = footprints_of_batch(batch)
            except (InvalidArgError, KeyError, TypeError, ValueError):
                # a record this builder cannot interpret is a record this
                # checker cannot read; refusing is the only sound answer, and
                # it is never `FRESH`
                return UNDECIDABLE("log-unreadable", tuple(degraded))
            hit = _scan_batch(fps, scope, step_id, seen, term_steps)
            if hit is None:
                return UNDECIDABLE("unknown-enum", tuple(degraded))
            for w in hit:
                total += 1
                # the cap is applied AFTER matched_on accounting, and after the
                # total — it can never change the verdict
                if len(witnesses) < witness_cap:
                    witnesses.append(w)
    except (StateError, OSError, ValueError):
        return UNDECIDABLE("log-unreadable", tuple(degraded))

    # 9
    receipt = (
        {"basis": scope.as_of_tt, "batches": exempt_count,
         "tt_range": [exempt_lo, exempt_hi], "theorem": "T1"}
        if exempt_count > 0 else None
    )
    if not witnesses:
        return FRESH(tuple(degraded), receipt)
    return POSSIBLY_STALE(witnesses, total, tuple(degraded), receipt)


def _scan_batch(fps: BatchFootprint, scope: DependencyScope, step_id: str | None,
                seen: set[tuple[str | None, str, int]],
                term_steps: Sequence[str | None] | None = None) -> list[Witness] | None:
    """One batch against one scope. `None` means a term refused.

    **Witnesses are deduplicated per `(step, batch_id, op_seq)`** — §13.6's
    worked example deduplicates the redundant `fp0c` match against `T1a`, where
    the carve arm of an op whose value arm already fired adds nothing a reader
    can act on. The step is part of the key rather than assumed constant,
    because a merged plan scope carries terms from several steps and one op
    hitting two of them is two facts, not one.
    """
    out: list[Witness] = []
    for fp in fps.ops:
        for index, term in enumerate(scope.terms):
            m, matched_on = intersects(term, fp)
            if m is Match.REFUSE:
                return None
            if m is not Match.HIT:
                continue
            sid = (term_steps[index] if term_steps is not None
                   and index < len(term_steps) else step_id)
            key = (sid, fps.batch_id, fp.seq)
            if key in seen:
                continue       # this op already has a witness for this step
            seen.add(key)
            out.append(Witness(
                batch_id=fps.batch_id, tt=fps.tt, op_seq=fp.seq, arm=fp.arm,
                cls=fp.cls, kind=fp.kind, identity=fp.identity.to_json(),
                vt=_witness_vt(fp), matched_term=index, matched_on=matched_on,
                step_id=sid))
    return out


# ---------------------------------------------------------------------------
# per-step checking (D5.4, T5.1) — the substrate M4.4's surface wraps
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class StepsVerdict:
    """Both granularities (D5.4, §13.8.4): one bit for the whole, plus the
    per-step map that says which operator's scope is loose.

    The headline is `.actionable_fresh`; the map is what §4.6 disaggregates.
    """

    per_step: tuple[tuple[str, Verdict], ...]

    @property
    def actionable_fresh(self) -> bool:
        return all(v.actionable_fresh for _sid, v in self.per_step)

    @property
    def witnesses(self) -> tuple[Witness, ...]:
        return tuple(w for _sid, v in self.per_step for w in v.witnesses)

    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(v.reason for _sid, v in self.per_step if v.reason is not None)

    def to_json(self) -> dict[str, Any]:
        return {"verdict": "fresh" if self.actionable_fresh else "possibly-stale",
                "steps": {sid: v.to_json() for sid, v in self.per_step}}


def check_steps(steps: Iterable[tuple[str, DependencyScope | dict[str, Any]]],
                log: EventLog, tt_now: int = OPEN_END, *,
                chain_cache: ChainCache | None = None,
                witness_cap: int = WITNESS_CAP) -> StepsVerdict:
    """Check each step against **its own scope and its own `tt_q`**, and fold
    (coordinator ruling, D-M4e).

    Both this and a merged-scope check are sound; per-step is *more precise*
    and gives `step_id` attribution for free. T5.1's induction is per-step:
    step *i* is unchanged if no op in `Σ(tt_q_i, τ]` meets `D_i`. The merged
    scope (D13.8) forces the **earliest** `tt_q` onto every term, which is the
    widening FF-7 required for a single scope object and is not required while
    the steps are still separate — so the merged check can never be `FRESH`
    where this one is `POSSIBLY_STALE`, and M4.4 tests exactly that.
    """
    return StepsVerdict(tuple(
        (sid, check(scope, log, tt_now, step_id=sid, chain_cache=chain_cache,
                    witness_cap=witness_cap))
        for sid, scope in steps))


#: The plan's vocabulary for the same object (§3.6b). `StepsVerdict` names what
#: it holds; `PlanVerdict` names where it is used.
PlanVerdict = StepsVerdict


def check_trace(record: dict[str, Any], log: EventLog, tt_now: int = OPEN_END, *,
                chain_cache: ChainCache | None = None,
                witness_cap: int = WITNESS_CAP) -> StepsVerdict:
    """A stored trace record against the log — the surface where the
    interesting answer lives.

    **Per-step is the production path** (D-M4e): each step is checked against
    its own scope and its own `tt_q`, and the results fold. T5.1's induction is
    per-step, and this is strictly more precise than checking the merged plan
    scope, which forces the **earliest** `tt_q` onto every term — the widening
    FF-7 required for a single scope object and one that is simply not needed
    while the steps are still separate. It also gives `step_id` attribution for
    free, which is what diagnoses *which* operator's scope is loose.

    **The merged scope is the fallback**, for a record carrying only
    `plan_basis` — an old record, or a result stored without its steps. It is
    equally sound and strictly coarser, and it cannot attribute: witnesses from
    a merged scope carry no `step_id`, because the merged object genuinely does
    not know which step contributed which term. The map is keyed `"plan"` so a
    reader can tell the two cases apart rather than seeing an invented id.

    A step that **failed** still contributes its scope (D13.14): it read
    whatever it read before it failed, and dropping it would narrow the plan's
    dependency to the steps that happened to succeed.
    """
    steps = [(str(s.get("step_id") or s.get("id") or f"s{i}"), s["dependency"])
             for i, s in enumerate(record.get("steps") or ())
             if isinstance(s.get("dependency"), dict)]
    if steps:
        return check_steps(steps, log, tt_now, chain_cache=chain_cache,
                           witness_cap=witness_cap)
    merged = record.get("dependency")
    if not isinstance(merged, dict):
        # a plan whose steps all failed before any basis was recorded carries no
        # basis at all (`TraceRecord.plan_basis` returns `{}` rather than
        # inventing one). There is nothing to check and nothing to certify.
        return StepsVerdict((("plan", UNDECIDABLE("no-tt_q")),))
    return StepsVerdict((("plan", check(merged, log, tt_now, chain_cache=chain_cache,
                                        witness_cap=witness_cap)),))


__all__ = [
    "CONJUNCTS", "ChainCache", "FRESH", "KnownOverlap", "Match", "POSSIBLY_STALE",
    "PlanVerdict", "REASONS", "State", "StepsVerdict", "UNDECIDABLE", "Verdict",
    "WITNESS_CAP", "Witness", "check", "check_steps", "check_trace", "incident_match",
    "intersects", "known_overlap", "meets", "targets_match", "vt_overlaps",
]
