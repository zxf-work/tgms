"""§4.2's property test: the derived scope is never narrower than a
from-scratch recompute's actual read.

The differential test in `tests/test_scope_derivations.py` samples;  this one
quantifies over the read itself. `RecordingAdapter` wraps a real
`StorageAdapter` and logs every call to the twelve methods that are the only
ways the ten rolled-out operators reach the store. Each recorded call reduces
to a *read region* (entity kind, uid/endpoint constraint, rel_types, vt
range), and for each region a representative witness op is synthesised and
run through the shipped `footprint.footprints_of_op` — so the props
vocabulary tested is the real one, not a hand-copied guess — and the shipped
`tgms.tgir.check.intersects`. The assertion is: every such witness footprint
must be admitted by at least one derived term.

This is *stronger* than the differential test in one respect (it quantifies
over the whole read region rather than sampling corrections) and weaker in
another (it cannot see a dependence that is not mediated by a recorded read):
`adapter.num_entities()`'s non-dependence for `temporal_reachability` and for
`graph_metric_timeseries`'s `reciprocity` metric would otherwise be flagged
as a missing node term, so those two calls are named in an explicit
allowlist at the call site of every test that needs it — never silently
skipped.

**Scope, stated up front rather than found by trial and error: this file
checks value-arm coverage.** The design's own soundness obligation (§1.1) is
that every footprint arm *that carries the effect* must be covered, not that
every arm a real op happens to emit must be — and for six of the nine
operators here the carve arm is deliberately excluded (`P = Pᵥ`) by a written
downstream argument (L9.1's carve-neutrality for instant counts, gate
Appendix A.3 for event-keyed reads, §9.3's content-equality argument for
`diff_snapshots`, §9.13's "the effect surfaces through the value arm's own
`@extent` instead" argument for `temporal_reachability`) that depends on what
the operator's *output* exposes, not on which storage columns a read
requested. A mechanical region reducer — which only ever sees adapter calls,
never the kernel's downstream arithmetic — cannot derive or check that
argument; asking it to would mean silently re-deriving D9.0 in code instead
of reading the proof that is supposed to license each `Pᵥ`. That proof is
exactly what `tests/test_scope_derivations.py`'s per-operator matrices check
explicitly, via `arms_that_hit`, per the rollout's own enablement checklist
(design §6 item 2) — this file is the read-side complement, not a
replacement for it.
"""

from __future__ import annotations

import dataclasses
import inspect
from dataclasses import dataclass
from typing import Any

import pytest

from tgms.core.model import OPEN_END
from tgms.temporal.algebra import REGISTRY, ensure_all_registered, validate_args
from tgms.tgir.check import Match, intersects
from tgms.tgir.footprint import footprints_of_op
from tgms.tgir.leaf import sigma_for
from tgms.tgir.leaves import terms_for

from tests.test_scope_derivations import _mk_store

ensure_all_registered()

#: The public read surface reachable from the ten rolled-out operators
#: (design §4.2), enumerated explicitly. `RecordingAdapter` binds a recording
#: wrapper for exactly these names, and only for the ones the wrapped
#: adapter actually implements (so `hasattr` still agrees with the real
#: adapter's capabilities, e.g. `aggregate_events_columnar` is absent on
#: DuckDB). Nothing else is ever bound: there is no `__getattr__`, so an
#: access to any other attribute — including a method added to
#: `StorageAdapter` later and never taught to this file — raises
#: `AttributeError` exactly as if it had never been implemented, rather than
#: silently passing through unrecorded.
READ_METHODS: tuple[str, ...] = (
    "dense_ids", "uids_for", "num_entities", "believed_node_versions",
    "nodes_columnar", "edges_columnar", "versions_page", "props_for_vids",
    "aggregate_events_columnar", "tcsr", "edge_idents_at", "resolve_entities",
)

#: Static schema constants two kernels read directly off `adapter.<NAME>`
#: rather than through a method call (`ops_versions.py:173,176`,
#: `ops_paths.py:298`). Content-free — forwarded as plain attributes, never
#: recorded as a read, because they carry no store state and cannot be
#: changed by any write.
_STATIC_ATTRS: tuple[str, ...] = ("VERSION_COLS", "VERSION_INT_COLS", "TCSR_COLS")


class RecordingAdapter:
    """Wraps a `StorageAdapter`. `self.calls` is `[(method_name,
    normalized_kwargs, return_value), ...]` in call order."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls: list[tuple[str, dict[str, Any], Any]] = []
        #: dense-id -> uid, populated opportunistically from every
        #: `dense_ids`/`uids_for` call, so a later `touching_ids=[...]`
        #: (dense ints) can be translated back to the uids a footprint's
        #: identity is spelled in.
        self.id_uid: dict[int, str] = {}
        for name in READ_METHODS:
            impl = getattr(inner, name, None)
            if impl is not None:
                setattr(self, name, self._recorder(name, impl))
        for name in _STATIC_ATTRS:
            if hasattr(inner, name):
                setattr(self, name, getattr(inner, name))

    def _recorder(self, name: str, impl: Any):
        try:
            sig: inspect.Signature | None = inspect.signature(impl)
        except (TypeError, ValueError):
            sig = None

        def wrapped(*args: Any, **kwargs: Any) -> Any:
            kw = _normalize(sig, args, kwargs)
            entry: list[Any] = [name, kw, None]
            self.calls.append(entry)  # type: ignore[arg-type]
            result = impl(*args, **kwargs)
            entry[2] = result
            return result

        return wrapped


def _normalize(sig: inspect.Signature | None, args: tuple, kwargs: dict) -> dict[str, Any]:
    if sig is not None:
        try:
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            return dict(bound.arguments)
        except TypeError:
            pass
    return {"_args": args, "_kwargs": kwargs}


# ---------------------------------------------------------------------------
# read regions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ReadRegion:
    """`(entity_kind, uid-or-endpoint constraint, rel_types, vt range)`
    (design §4.2). `None` on `entity_kind`/`uids`/`rel_types` means
    unconstrained — every kind, every identity, every rel_type. `vt` is
    always a concrete `(lo, hi)`; a call with no bound at all reduces to
    `(0, OPEN_END)`, which is what forces a witness placed far from any real
    data — the only thing that can still catch it is `V = TOP`.

    `mode` matters only for an edge region with `uids` bound: `"both"`
    mirrors `touching_both=True` (both endpoints must be in the set — the
    witness is one edge with both endpoints drawn from the set), `"either"`
    mirrors the OR pre-filter (a separate witness per endpoint role).

    `existence_only` marks a region whose hazard is *identity coming into
    existence* (`dense_ids`, `uids_for`, `num_entities` — every one of them
    changes only when `ensure_entities` runs) rather than a read of believed
    *content*. `correct`/`retract` can never register a new identity
    (§1.8), so a witness for such a region must not include them — doing so
    would ask the existence pair to cover a write class it is explicitly,
    soundly, not designed to cover.
    """

    entity_kind: str | None
    uids: frozenset[str] | None
    rel_types: frozenset[str] | None
    vt: tuple[int, int]
    mode: str = "either"
    existence_only: bool = False


def _vt_of(vt_min: Any, vt_max: Any) -> tuple[int, int]:
    lo = 0 if vt_min is None else int(vt_min)
    hi = OPEN_END if vt_max is None else int(vt_max)
    return (lo, hi)


def _resolve_ids(id_uid: dict[int, str], ids: Any) -> frozenset[str] | None:
    """Best-effort dense-id -> uid translation via the cache built from
    `dense_ids`/`uids_for` calls already seen. `None` (unconstrained) when
    any id cannot be resolved — the conservative direction: the property
    test then requires the scope to cover the whole entity kind rather than
    silently under-covering an id it could not name."""
    out: set[str] = set()
    for i in ids:
        u = id_uid.get(int(i))
        if u is None:
            return None
        out.add(u)
    return frozenset(out)


def _reduce_dense_ids(kw: dict, result: Any, id_uid: dict[int, str]) -> list[ReadRegion]:
    uids = list(kw["uids"])
    if result is not None:
        ids = result.tolist() if hasattr(result, "tolist") else list(result)
        for u, i in zip(uids, ids):
            id_uid[int(i)] = u
    if not uids:
        return []
    # existence: the write may register the uid at ANY valid-time location
    return [ReadRegion(None, frozenset(uids), None, (0, OPEN_END), existence_only=True)]


def _reduce_uids_for(kw: dict, result: Any, id_uid: dict[int, str]) -> list[ReadRegion]:
    """No region at all: `uids_for` maps an already-assigned dense id back to
    its uid string, a permanent mapping (dense ids never disappear or get
    reassigned) over identities already drawn from a prior
    `edges_columnar`/`nodes_columnar`/`dense_ids` call in the same
    invocation — which is what actually produced those ids and whose own
    region already dominates this one. Still updates the id->uid cache,
    which is the whole reason to record this call at all (`touching_ids`
    elsewhere needs it)."""
    ids = list(kw["ids"])
    uids = list(result) if result is not None else []
    for i, u in zip(ids, uids):
        id_uid[int(i)] = u
    return []


def _reduce_num_entities(kw: dict, result: Any, id_uid: dict[int, str]) -> list[ReadRegion]:
    # only ensure_entities moves this count; correct/retract never do
    return [ReadRegion(None, None, None, (0, OPEN_END), existence_only=True)]


def _reduce_believed_node_versions(kw: dict, result: Any,
                                   id_uid: dict[int, str]) -> list[ReadRegion]:
    return [ReadRegion("node", frozenset({kw["uid"]}), None, (0, OPEN_END))]


def _reduce_nodes_columnar(kw: dict, result: Any, id_uid: dict[int, str]) -> list[ReadRegion]:
    return [ReadRegion("node", None, None, _vt_of(kw.get("vt_min"), kw.get("vt_max")))]


def _reduce_edges_columnar(kw: dict, result: Any, id_uid: dict[int, str]) -> list[ReadRegion]:
    touching = kw.get("touching_ids")
    uids = None if not touching else _resolve_ids(id_uid, touching)
    rel = kw.get("rel_types")
    rel_set = frozenset(rel) if rel else None
    mode = "both" if kw.get("touching_both") else "either"
    return [ReadRegion("edge", uids, rel_set, _vt_of(kw.get("vt_min"), kw.get("vt_max")), mode)]


def _reduce_versions_page(kw: dict, result: Any, id_uid: dict[int, str]) -> list[ReadRegion]:
    rel = kw.get("rel_types")
    rel_set = frozenset(rel) if rel else None
    return [ReadRegion(kw["kind"], None, rel_set, (int(kw["t_a"]), int(kw["t_b"])))]


def _reduce_props_for_vids(kw: dict, result: Any, id_uid: dict[int, str]) -> list[ReadRegion]:
    # keyed by vid, never by identity or valid time: every candidate vid was
    # already drawn from a nodes_columnar/edges_columnar call in the same
    # invocation, whose region already dominates this one
    return []


def _reduce_aggregate_events_columnar(kw: dict, result: Any,
                                      id_uid: dict[int, str]) -> list[ReadRegion]:
    touching = kw.get("touching_ids")
    uids = None if not touching else _resolve_ids(id_uid, touching)
    rel = kw.get("rel_types")
    rel_set = frozenset(rel) if rel else None
    return [ReadRegion("edge", uids, rel_set, _vt_of(kw.get("t_a"), kw.get("t_b")))]


def _reduce_tcsr(kw: dict, result: Any, id_uid: dict[int, str]) -> list[ReadRegion]:
    # the cached whole-store current-belief TCSR: unwindowed by construction
    return [ReadRegion("edge", None, None, (0, OPEN_END))]


def _reduce_edge_idents_at(kw: dict, result: Any, id_uid: dict[int, str]) -> list[ReadRegion]:
    # resolves eid/rel_type for row addresses a prior columnar read already
    # produced; never reached on the DuckDB backend these tests run against
    # (TCSR_COLS always includes "eid", so ops_paths.py's `if "eid" in cols`
    # branch is always taken)
    return []


def _reduce_resolve_entities(kw: dict, result: Any, id_uid: dict[int, str]) -> list[ReadRegion]:
    return [ReadRegion(None, None, None, (0, OPEN_END))]


_REDUCERS = {
    "dense_ids": _reduce_dense_ids,
    "uids_for": _reduce_uids_for,
    "num_entities": _reduce_num_entities,
    "believed_node_versions": _reduce_believed_node_versions,
    "nodes_columnar": _reduce_nodes_columnar,
    "edges_columnar": _reduce_edges_columnar,
    "versions_page": _reduce_versions_page,
    "props_for_vids": _reduce_props_for_vids,
    "aggregate_events_columnar": _reduce_aggregate_events_columnar,
    "tcsr": _reduce_tcsr,
    "edge_idents_at": _reduce_edge_idents_at,
    "resolve_entities": _reduce_resolve_entities,
}


# ---------------------------------------------------------------------------
# per-operator region refinements — named, argued exemptions, same spirit as
# the num_entities allowlist
# ---------------------------------------------------------------------------
#
# A region reduced from the raw adapter call is an upper bound on what a
# kernel is sensitive to: three of the nine operators read more broadly at
# the adapter boundary than they are actually sensitive to, because they
# narrow further in their OWN Python logic after the call returns — a mask,
# an exact-endpoint filter, or a traversal bound the adapter call itself
# cannot see. Treating the raw call's region as the truth there would ask the
# derived scope to cover corrections the kernel provably cannot see, which is
# not what obligation O asks for (only footprints that carry an effect the
# answer depends on). Each narrowing below cites the same source the leaves.py
# docstring for that derivation cites, so it is checkable against the kernel,
# not asserted on faith.

def _refine_temporal_paths(filled: dict, regions: list[ReadRegion]) -> list[ReadRegion]:
    """`ops_paths.py:290-291`: the cached, unwindowed current-belief TCSR
    branch reads with no vt bound at all, but the traversal constraints
    (`tau < vt_e`, `tau < t_b`, `tau >= t_a` via the source arrival) make it
    read-equivalent to the windowed scan -- design §9.8's "domain follows
    the answer", this rollout's second invocation."""
    w = filled.get("window")
    if not isinstance(w, dict):
        return regions
    vt = (int(w["t_a"]), int(w["t_b"]))
    return [dataclasses.replace(r, vt=vt) if r.vt == (0, OPEN_END) else r for r in regions]


def _refine_burst_detection(filled: dict, regions: list[ReadRegion]) -> list[ReadRegion]:
    """`ops_series.py:268-273`: `node_activity` reads the whole windowed
    edge scan with no `touching_ids` pushdown at all, then masks in Python
    to `(src_id == uid_id) | (dst_id == uid_id)` — the kernel's own mask the
    derivation's `incident("either", uid)` arm is built to match exactly."""
    target = filled.get("target")
    if not isinstance(target, dict) or target.get("kind") != "node_activity":
        return regions
    uid = target.get("uid")
    if not uid:
        return regions
    return [dataclasses.replace(r, uids=frozenset({uid}))
            if r.entity_kind == "edge" and r.uids is None and not r.existence_only
            else r for r in regions]


def _refine_co_active(filled: dict, regions: list[ReadRegion]) -> list[ReadRegion]:
    """`ops_series.py:357-366`: `_select`'s `touching_ids` pushdown is an OR
    pre-filter over whichever endpoints a spec names; the exact mask applied
    afterward (`e["src_id"] == src_id`, `e["dst_id"] == dst_id`, both when
    both are named) is what actually decides survival, so a spec naming
    both endpoints is sensitive only to that exact pair. This mirrors
    `_co_active_spec_term` exactly, because the true dependency here IS that
    per-spec construction, not the raw scan's broader pushdown."""
    specs = [s for s in (filled.get("a_spec"), filled.get("b_spec")) if isinstance(s, dict)]
    if not specs:
        return regions
    replacement = []
    for spec in specs:
        rel = spec.get("rel_type")
        rel_set = frozenset({rel}) if rel else None
        src, dst = spec.get("src"), spec.get("dst")
        if src and dst:
            uids, mode = frozenset({src, dst}), "both"
        elif src:
            uids, mode = frozenset({src}), "src"
        elif dst:
            uids, mode = frozenset({dst}), "dst"
        else:
            uids, mode = None, "either"
        replacement.append(ReadRegion("edge", uids, rel_set, (0, OPEN_END), mode))
    kept = [r for r in regions if not (r.entity_kind == "edge" and not r.existence_only)]
    return kept + replacement


_REGION_REFINERS = {
    "temporal_paths": _refine_temporal_paths,
    "burst_detection": _refine_burst_detection,
    "co_active": _refine_co_active,
}


# ---------------------------------------------------------------------------
# witness synthesis (real footprints, via the shipped builder)
# ---------------------------------------------------------------------------

def _witness_ops(region: ReadRegion) -> list[dict[str, Any]]:
    """One representative op record per write shape a region admits —
    `assert_node`/`assert_edge`, `correct`, `retract`, `ingest_events` —
    fed to the shipped `footprints_of_op` so the props vocabulary tested is
    the real one."""
    lo, hi = region.vt
    # fully unconstrained (no bound was ever passed): place the witness far
    # from any real test data, so only vt = TOP could still catch it
    vt_s = 500_000 if lo == 0 and hi >= OPEN_END else lo
    vt_e = vt_s + 1

    ops: list[dict[str, Any]] = []
    if region.entity_kind in (None, "node"):
        uid = next(iter(region.uids)) if region.uids else "outside-witness-node"
        ops.append({"op": "assert_node", "uid": uid, "vt_s": vt_s, "vt_e": vt_e})
        if not region.existence_only:
            ops.append({"op": "correct", "ref": {"kind": "node", "uid": uid},
                       "props": {"w": 1}, "vt_s": vt_s, "vt_e": vt_e})
            ops.append({"op": "retract", "ref": {"kind": "node", "uid": uid}, "t": vt_s})
        ops.append({"op": "ingest_events", "offset": 0, "events": [],
                   "nodes": [{"uid": uid, "label": "X", "vt_s": vt_s, "vt_e": vt_e}]})
    if region.entity_kind in (None, "edge"):
        rel = next(iter(region.rel_types)) if region.rel_types else "outside-witness-rel"
        other = "outside-witness-endpoint"
        if region.uids and region.mode == "both":
            members = list(region.uids)
            pairs = [(members[0], members[1] if len(members) > 1 else members[0])]
        elif region.uids and region.mode == "src":
            # role="src" checks the footprint's src field only (D13.23) — an
            # edge naming this uid as dst alone is a genuine non-match, so
            # only the src-role pairing is a real witness
            pairs = [(next(iter(region.uids)), other)]
        elif region.uids and region.mode == "dst":
            pairs = [(other, next(iter(region.uids)))]
        elif region.uids:
            member = next(iter(region.uids))
            pairs = [(member, other), (other, member)]
        else:
            pairs = [("outside-witness-src", "outside-witness-dst")]
        for src, dst in pairs:
            ref = {"kind": "edge", "src": src, "dst": dst, "rel_type": rel, "disc": ""}
            ops.append({"op": "assert_edge", "src": src, "dst": dst, "rel_type": rel,
                       "disc": "", "vt_s": vt_s, "vt_e": vt_e})
            if not region.existence_only:
                ops.append({"op": "correct", "ref": ref, "props": {"w": 1},
                           "vt_s": vt_s, "vt_e": vt_e})
                ops.append({"op": "retract", "ref": ref, "t": vt_s})
            ops.append({"op": "ingest_events", "offset": 0,
                       "events": [{"src": src, "dst": dst, "rel_type": rel, "vt_s": vt_s}]})
    return ops


def witnesses_of(regions: list[ReadRegion]) -> list:
    """Real value-arm footprints, filtered to the region's own entity kind.

    `footprints_of_op` is total over one *op record*, not over one region:
    an `ingest_events` witness carrying edge events also carries an
    unconditional node arm (D13.22 — the builder cannot know from the log
    alone whether an endpoint was new), even when the region under test is
    edge-only. That second arm is a real effect of a *different* write this
    op happens to also make, not a claim about what THIS region's read
    depends on, so it is dropped here rather than asked of a term that was
    never obliged to cover it. An unconstrained region (`entity_kind is
    None`, existence-style) keeps both kinds — either can register a dense
    id, which is exactly the hazard such a region names.

    The carve arm is dropped everywhere (module docstring: this file checks
    value-arm coverage; the carve-arm verdict is the hand-written matrices'
    job).
    """
    out = []
    for region in regions:
        for op in _witness_ops(region):
            for fp in footprints_of_op(op):
                if fp.arm == "carve":
                    continue
                if region.entity_kind is not None and fp.entity_kind != region.entity_kind:
                    continue
                out.append(fp)
    return out


# ---------------------------------------------------------------------------
# the assertion
# ---------------------------------------------------------------------------

def assert_scope_covers_reads(op: str, args: dict[str, Any], adapter: Any, *,
                              allow: frozenset[str] = frozenset()) -> int:
    """Run the operator's own kernel (not `call_operator`'s envelope — the
    cost/freshness bookkeeping around it reads adapter methods no derivation
    needs to know about) against a `RecordingAdapter`, reduce every recorded
    read to a region, and assert every witness footprint synthesised from
    those regions is admitted by the derived scope. Returns the witness
    count, so a test can also assert it is not accidentally zero."""
    filled = validate_args(op, dict(args))
    terms = terms_for(op, filled, sigma_for(op, filled))
    rec = RecordingAdapter(adapter)
    REGISTRY[op].fn(rec, filled)
    regions: list[ReadRegion] = []
    for name, kw, result in rec.calls:
        if name in allow:
            continue
        regions.extend(_REDUCERS[name](kw, result, rec.id_uid))
    refine = _REGION_REFINERS.get(op)
    if refine is not None:
        regions = refine(filled, regions)
    fps = witnesses_of(regions)
    assert fps, f"{op}{filled}: no read region produced a witness footprint"
    for fp in fps:
        assert any(intersects(t, fp)[0] is Match.HIT for t in terms), \
            (op, filled, fp)
    return len(fps)


# ---------------------------------------------------------------------------
# the nine rolled-out operators, one representative call per branch
# ---------------------------------------------------------------------------

W = {"t_a": 0, "t_b": 900}

CASES: list[tuple[str, dict[str, Any], frozenset[str]]] = [
    ("count_temporal_motifs",
     {"motif": "M_2node_pingpong", "delta": 50, "window": W}, frozenset()),
    ("count_temporal_motifs",
     {"motif": "M_2node_pingpong", "delta": 50, "window": W,
      "node_filter": ["n0", "n1", "n2"]}, frozenset()),
    ("find_temporal_motif_instances",
     {"motif": "M_2node_pingpong", "delta": 50, "window": W}, frozenset()),
    ("temporal_reachability", {"src": "n0", "window": W}, frozenset({"num_entities"})),
    ("temporal_paths", {"src": "n0", "dst": "n3", "window": W}, frozenset()),
    ("burst_detection",
     {"target": {"kind": "edge_event_rate"}, "window": W, "stride": 10}, frozenset()),
    ("burst_detection",
     {"target": {"kind": "node_activity", "uid": "n0"}, "window": W, "stride": 10},
     frozenset()),
    ("graph_metric_timeseries",
     {"metric": "edge_event_count", "window": W, "stride": 10}, frozenset()),
    ("graph_metric_timeseries",
     {"metric": "active_edge_count", "window": W, "stride": 10}, frozenset()),
    ("graph_metric_timeseries",
     {"metric": "reciprocity", "window": W, "stride": 10}, frozenset({"num_entities"})),
    ("graph_metric_timeseries",
     {"metric": "node_count", "window": W, "stride": 10}, frozenset()),
    ("graph_metric_timeseries",
     {"metric": "new_node_rate", "window": W, "stride": 10}, frozenset()),
    ("graph_metric_timeseries",
     {"metric": "mean_out_degree", "window": W, "stride": 10}, frozenset()),
    ("co_active",
     {"a_spec": {"rel_type": "R"}, "b_spec": {"rel_type": "S"},
      "allen_relation": {"relation": "overlaps"}}, frozenset()),
    ("co_active",
     {"a_spec": {"src": "n0", "dst": "n1"}, "b_spec": {"src": "n2"},
      "allen_relation": {"relation": "before", "gap": 5}}, frozenset()),
    ("diff_snapshots", {"t1": 100, "t2": 500}, frozenset()),
    ("diff_snapshots",
     {"t1": 100, "t2": 500, "scope": {"seeds": ["n0"], "hops": 1}}, frozenset()),
    ("version_history", {"kind": "node", "window": W}, frozenset()),
    ("version_history", {"kind": "edge", "window": W}, frozenset()),
    ("version_history", {"kind": "edge", "window": W, "rel_types": ["R"]}, frozenset()),
    ("snapshot_subgraph", {"seeds": ["n0"], "t_valid": 50, "hops": 1}, frozenset()),
]


@pytest.fixture(scope="module")
def store():
    a = _mk_store()
    yield a
    a.close()


@pytest.mark.parametrize(
    "op,args,allow", CASES,
    ids=[f"{op}#{args.get('metric') or args.get('kind') or i}" for i, (op, args, _) in enumerate(CASES)])
def test_scope_covers_the_actual_read(store, op, args, allow):
    n = assert_scope_covers_reads(op, args, store, allow=allow)
    assert n > 0


def test_allowlist_entries_are_actually_exercised(store):
    """An allowlist entry is a claim someone can check, not a silent skip
    (§4.2). Both call sites that name `num_entities` must actually see it
    recorded, or the exemption is vacuous."""
    filled = validate_args("temporal_reachability", {"src": "n0", "window": W})
    rec = RecordingAdapter(store)
    REGISTRY["temporal_reachability"].fn(rec, filled)
    assert any(name == "num_entities" for name, _kw, _r in rec.calls)

    filled = validate_args("graph_metric_timeseries",
                           {"metric": "reciprocity", "window": W, "stride": 10})
    rec = RecordingAdapter(store)
    REGISTRY["graph_metric_timeseries"].fn(rec, filled)
    assert any(name == "num_entities" for name, _kw, _r in rec.calls)


def test_recording_adapter_fails_on_an_unrecorded_attribute():
    """The property this whole file exists to buy: a `__getattr__`-based
    proxy would silently pass a method it was never taught about straight
    through. This one has no `__getattr__` at all, so anything outside
    `READ_METHODS` — including ordinary write/transaction methods a kernel
    has no business calling — raises `AttributeError`."""
    rec = RecordingAdapter(_mk_store())
    with pytest.raises(AttributeError):
        rec.apply_ops([], 1)
    with pytest.raises(AttributeError):
        rec.begin()
    with pytest.raises(AttributeError):
        rec.some_future_method_nobody_taught_this_file_about()
