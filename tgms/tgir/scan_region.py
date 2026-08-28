"""`ScanRegion` — the Level-1 recorded scan region for `PatternMatch`
(`docs/design/M5_LEVEL1_SOUNDNESS.md` §1.2, §4.1, `M5_DESIGN.md` §4.3/§7.4).

**The fail-safe governs every function in this module** (`M5_DESIGN.md` §4.3):
*"An absent scan region means no narrowing. The artifact falls back to its
Level-0 terms. A kernel that bailed out, refused under D-155, or was not
instrumented records nothing and loses precision, never soundness."* Every
constructor and every parse path here therefore has exactly one failure
behavior on anything it cannot interpret — produce nothing usable — never a
raise that reaches a recorder, and never a widened term substituted for a
refused one. `scan_region_terms()` is the single function every consumer
(`tgms.tgir.level1`, `tgms.artifact.witness`) calls; on `None`, on a dict that
does not parse, on a future schema version, or on `complete: false`, it
returns `()` — the empty disjunction is not a claim, it is "nothing recorded".

**The region is a *pair*, not the node arm alone**
(`M5_LEVEL1_SOUNDNESS.md` §1.4, L-PM1's corollary): `scan_region_terms()`
always emits the intensional per-unbound-edge-variable descriptors (one
`ScopeTerm` per `EdgeDomain`, `targets.edges` pinned to `TOP` — never
narrowed to observed `eid`s) *alongside* the extensional node-uid term, never
the node term by itself. Recording the node arm without the edge arm would be
exactly `burst_detection`'s trap (`FRESHNESS_SEMANTICS.md:1633-1634`) — PO-P4
names it and this is the code that refuses to reconstruct it: there is no
public constructor that can produce a node-only region, `ScanRegion` always
carries `edge_domains` and `scan_region_terms()` always walks it first.

`tgms/tgir/scan_region.py` is on `scripts/check_freshness_boundary.py`'s
per-module allowlist: `tgms.core.model`, `tgms.core.errors`,
`tgms.tgir.depscope`, and nothing else from `tgms`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from tgms.core.errors import InvalidArgError
from tgms.core.model import canonical_json, digest
from tgms.tgir.depscope import TOP, ScopeTerm, Targets

SCHEMA_NAME = "tgms-scan-region"

#: An integer. A reader that does not recognize a region's version refuses to
#: interpret it — `scan_region_terms()` returns `()`, never a term built from
#: a guess at what a future version means (mirrors `depscope.py:36-40`).
SCHEMA_VERSION = 1

#: §1.2: whether `eval_pattern._domain` issued a fresh `EdgeScan`
#: (`pattern.py:112-113`) or read an already-materialized `sources` relation
#: (`pattern.py:109-111`, `:121-129`) for this edge variable.
EDGE_SOURCES: tuple[str, ...] = ("scan", "bound")

__all__ = [
    "EDGE_SOURCES", "SCHEMA_NAME", "SCHEMA_VERSION", "EdgeDomain", "ScanRegion",
    "pattern_match_region", "scan_region_terms",
]


@dataclass(frozen=True, slots=True)
class EdgeDomain:
    """One edge variable's intensional descriptor (§1.2). `var` names the
    pattern variable; `source` is `"scan"` when the domain came from a store
    read and `"bound"` when it came from an already-materialized `sources`
    relation; `rel_type` is the single declared type, or `None` when the
    `edge_pat` left it open (`pattern.py:112`, `rel_types=None` scans every
    type — FM-S6/FM-5's "an undeclared variable scans all types" applies
    verbatim here)."""

    var: str
    source: str
    rel_type: str | None = None

    def __post_init__(self) -> None:
        if not self.var:
            raise InvalidArgError("an edge domain needs a variable name")
        if self.source not in EDGE_SOURCES:
            raise InvalidArgError(f"unknown edge domain source: {self.source!r}",
                                  allowed=list(EDGE_SOURCES))

    def to_json(self) -> dict[str, Any]:
        return {"var": self.var, "source": self.source, "rel_type": self.rel_type}

    @staticmethod
    def from_json(obj: Any) -> "EdgeDomain":
        if not isinstance(obj, dict):
            raise InvalidArgError("an edge domain must be an object", got=obj)
        var, source = obj.get("var"), obj.get("source")
        if not isinstance(var, str) or not var:
            raise InvalidArgError("an edge domain needs a string var", got=var)
        if source not in EDGE_SOURCES:
            raise InvalidArgError(f"unknown edge domain source: {source!r}",
                                  allowed=list(EDGE_SOURCES))
        rel_type = obj.get("rel_type")
        return EdgeDomain(var, source, str(rel_type) if rel_type is not None else None)


@dataclass(frozen=True, slots=True)
class ScanRegion:
    """§1.2's recorded region for one `PatternMatch` execution.

    `t_v`/`t_b` are `node.sigma`'s own window (`types.py:252-267`), copied
    **unadjusted** per D13.6/PO-P1 — never a hull, never an observed
    `min(vt_s)/max(vt_e)` extent (§1.6 FM-4 names that trap by citation).

    `complete` is the recorder's own honesty bit. `eval_pattern` sets it via
    `pattern_match_region` only at its single normal-return site
    (`pattern.py:61`); every widening condition (W-P1..W-P6) is instead a
    reason the *caller* never builds a `ScanRegion` at all — an exception
    unwinds before the constructor runs, and `region_sink` stays empty. A
    region somehow built with `complete=False` (there is no public path that
    does; the field exists so a wire-parsed object states its own honesty
    bit rather than a reader having to infer it) is treated identically to no
    region at all by `scan_region_terms`.
    """

    node_digest: str
    t_v: tuple[tuple[int, int], ...]
    t_b: int
    edge_domains: tuple[EdgeDomain, ...]
    node_uids: dict[str, tuple[str, ...]]
    node_cohorts: dict[str, tuple[str, ...]] = field(default_factory=dict)
    complete: bool = False
    op: str = "PatternMatch"
    schema: str = SCHEMA_NAME
    version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.op != "PatternMatch":
            raise InvalidArgError(f"unknown scan-region op: {self.op!r}",
                                  allowed=["PatternMatch"])
        if not self.node_digest:
            raise InvalidArgError("a scan region needs a node_digest")
        for iv in self.t_v:
            if not (isinstance(iv, tuple) and len(iv) == 2 and 0 <= iv[0] < iv[1]):
                raise InvalidArgError("a sigma.t_v entry is a [a, b) pair", got=iv)
        # PO-P4's corollary, enforced at construction: the region is a *pair*
        # — the intensional edge descriptors carry every absence dependence
        # (§1.4 point 1) — so a `complete` region with a node arm and no edge
        # arm at all is exactly the shape `burst_detection`'s trap names
        # (`:1633-1634`) and must never be buildable, not merely
        # under-narrowed. Every real `PatternMatch` has >= 1 `edge_pat`
        # (`node.py`'s `Pattern.__post_init__`), so this never fires on a
        # genuine recording; it exists to make the trap a `raise`, not a
        # silent unsoundness, for any other caller.
        if self.complete and not self.edge_domains:
            raise InvalidArgError(
                "a complete PatternMatch scan region needs at least one edge "
                "domain — a node-only region is the burst_detection trap "
                "(PO-P4), not a valid recording")

    def to_json(self) -> dict[str, Any]:
        """Deterministic, versioned (§4's "region serialization" obligation):
        every mapping is sorted by key before it is walked, so two builds of
        the same region canonicalize identically regardless of dict
        insertion order."""
        return {
            "schema": self.schema,
            "version": self.version,
            "op": self.op,
            "node_digest": self.node_digest,
            "complete": self.complete,
            "sigma": {"t_v": [list(iv) for iv in self.t_v], "t_b": self.t_b},
            "edge_domains": [e.to_json() for e in self.edge_domains],
            "node_uids": {k: sorted(v) for k, v in sorted(self.node_uids.items())},
            "node_cohorts": {k: sorted(v) for k, v in sorted(self.node_cohorts.items())},
        }

    def canonical(self) -> str:
        return canonical_json(self.to_json())

    def digest(self) -> str:
        return digest(self.to_json())

    @staticmethod
    def from_json(obj: Any) -> "ScanRegion | None":
        """`None` on anything this reader does not recognize — the
        refuse-not-widen discipline `DependencyScope`'s own version gate uses
        (`depscope.py:36-40`), restated here because a malformed or
        future-versioned region must cost precision, never soundness. Never
        raises: a caller (`level1.refine`, `scan_region_terms`) can hand this
        whatever `annotations[...]["scan_region"]` held, verbatim."""
        if not isinstance(obj, dict):
            return None
        if obj.get("schema") != SCHEMA_NAME or obj.get("version") != SCHEMA_VERSION:
            return None
        if obj.get("op") != "PatternMatch":
            return None
        try:
            sigma = obj["sigma"]
            t_v = tuple((int(a), int(b)) for a, b in sigma["t_v"])
            t_b = int(sigma["t_b"])
            edge_domains = tuple(EdgeDomain.from_json(e) for e in obj.get("edge_domains") or ())
            node_uids = {str(k): tuple(str(u) for u in v)
                         for k, v in (obj.get("node_uids") or {}).items()}
            node_cohorts = {str(k): tuple(str(u) for u in v)
                            for k, v in (obj.get("node_cohorts") or {}).items()}
            return ScanRegion(
                node_digest=str(obj["node_digest"]), t_v=t_v, t_b=t_b,
                edge_domains=edge_domains, node_uids=node_uids, node_cohorts=node_cohorts,
                complete=bool(obj.get("complete", False)))
        except (KeyError, TypeError, ValueError, InvalidArgError):
            return None


def pattern_match_region(*, node_digest: str, t_v: Iterable[tuple[int, int]], t_b: int,
                         edge_domains: Iterable[EdgeDomain],
                         node_uids: dict[str, Iterable[str]],
                         node_cohorts: dict[str, Iterable[str]] | None = None) -> ScanRegion:
    """The one constructor `tgms/tgir/eval/pattern.py` calls, and only at
    `eval_pattern`'s normal return (`pattern.py:61`) — never on a raise, never
    on a D-155 refusal (both unwind before this is reached). `complete` is
    always `True` here: there is no other call site, so there is no path that
    needs it `False`."""
    return ScanRegion(
        node_digest=node_digest,
        t_v=tuple((int(a), int(b)) for a, b in t_v),
        t_b=int(t_b),
        edge_domains=tuple(edge_domains),
        node_uids={k: tuple(str(u) for u in v) for k, v in node_uids.items()},
        node_cohorts={k: tuple(str(u) for u in v) for k, v in (node_cohorts or {}).items()},
        complete=True)


def scan_region_terms(region: Any) -> tuple[ScopeTerm, ...]:
    """§1.2's table: `ScanRegion(PatternMatch)` -> `(T_edge(v)*, T_node)`.

    Accepts a `ScanRegion`, the raw dict `annotations[nd]["scan_region"]`
    would hold, or `None`. Returns `()` — never raises — on anything the
    fail-safe covers: `None`, a dict `ScanRegion.from_json` cannot parse, or
    `complete is False`. A caller therefore never needs its own version check
    before calling this.

    Every `T_edge(v)` — whether `source == "scan"` or `source == "bound"` —
    carries `targets.edges = TOP` (§1.2's table, both rows identical; W-P4:
    the `bound` arm is *never* narrowed to observed `eid`s in v1). The only
    per-variable narrowing is `rel_types`, when the `edge_pat` declared one.
    `T_node` is the single extensional term, `targets.nodes` the union of
    every node variable's recorded uid set — admissible only *because* it
    always accompanies the edge terms above (L-PM1's corollary; see this
    module's docstring).
    """
    if isinstance(region, dict):
        region = ScanRegion.from_json(region)
    if not isinstance(region, ScanRegion) or not region.complete:
        return ()
    # PO-P4, defense in depth (`ScanRegion.__post_init__` already refuses to
    # construct this shape; this guard is the consumption-side half of "never
    # constructible/consumed" for any region that reaches here some other
    # way, e.g. a future wire format this reader parses too permissively).
    if not region.edge_domains:
        return ()

    #: PO-P1: Σ's window, copied unadjusted. An empty `t_v` cannot occur from
    #: `pattern_match_region` (`node.sigma.t_v` is never empty — `Sigma.
    #: __post_init__`), but treating it as `TOP` rather than as "matches
    #: nothing" keeps this function widening-only on any input, not just the
    #: ones its own constructor produces.
    vt = region.t_v if region.t_v else TOP

    terms: list[ScopeTerm] = []
    for edge in region.edge_domains:
        terms.append(ScopeTerm(
            kinds=TOP, targets=Targets(edges=TOP),
            rel_types=(edge.rel_type,) if edge.rel_type is not None else TOP,
            vt=vt, vt_mode="overlap", props=TOP))

    all_uids = tuple(sorted({u for uids in region.node_uids.values() for u in uids}))
    terms.append(ScopeTerm(
        kinds=TOP, targets=Targets(nodes=all_uids),
        rel_types=TOP, vt=vt, vt_mode="overlap", props=TOP))
    return tuple(terms)
