"""`PatternMatch` — §2.9, R1: general multi-way matching, **fixed length**.

**Execution strategy: staged expansion joined on node identity.** Each
`edge_pat` has a domain relation — its `sources` restriction where one is
bound, an `EdgeScan` under Σ otherwise. The search binds one edge pattern at a
time, choosing at each step the pattern that shares the most already-bound node
variables, and joins it into the running binding relation by hash on those
shared endpoints. §6's BI11 note records that the staged plan and one triangle
match are **the same relation**, so this is an execution strategy rather than a
semantic choice; `test_tgir_eval_pattern.py` proves it on a small fixture by
comparing against a brute-force enumeration.

**Edge-isomorphism over identities, not versions** (§8.5 CLOSED). No two edge
variables may bind versions of the same `eid`; node variables are **not**
implicitly distinct. The version-based reading would let two carve fragments of
one logical edge bind two variables, so a correction that changed no property
value would *manufacture* pattern instances no uncorrected store has — a CE-5
analogue at the pattern level, and exactly the class of surprise this system
exists to prevent.

**`sources` rebinds, it does not match prefixes** (RG-8): an edge variable takes
`r`'s edge identity column and a node variable takes `r`'s node identity column;
where `r` carries more than one of that kind the binding must name it, which the
node layer already enforces at construction.

**No worst-case-optimal join in v1.** bo33's nine-edge motif over a `rating > 0`
domain on 35k edges is the largest instance in the corpus and is what this has
to clear.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import numpy as np

from tgms.tgir.eval.scan import scan_nodes
from tgms.tgir.node import EdgePat, EdgeScan, NodeScan, PatternMatch
from tgms.tgir.relation import Relation
from tgms.tgir.types import EDGE_COLUMNS, PATTERN_NODE_COLUMNS, Column, Schema

#: The edge columns the search itself needs, whatever the plan projects.
_KEY_COLUMNS = ("eid", "src", "dst", "vt_s", "vid")


def eval_pattern(node: PatternMatch, sources: dict[str, Relation], adapter: Any,
                 live: frozenset[str] | None = None,
                 budget: Any = None, scans: Any = None) -> Relation:
    domains = {edge.var: _domain(node, edge, sources, adapter)
               for edge in node.pattern.edge_pats}
    node_domains = _node_domains(node, sources)

    order = _search_order(node, domains)
    bindings = _Bindings(node_domains)
    for edge in order:
        bindings.extend(edge, domains[edge.var])
        if budget is not None:
            budget.charge(bindings.n)

    return _materialize(node, bindings, adapter, live, scans)


def _node_domains(node: PatternMatch,
                  sources: dict[str, Relation]) -> dict[str, set]:
    """`sources` on a **node** variable restricts that variable's uids.

    §2.9 makes the pushed and un-pushed forms semantically identical, so a node
    cohort has to actually restrict: without this the entry was validated,
    evaluated and then ignored, and a plan naming an empty cohort still
    returned matches. BI11's cohort (`a, b, c IN personsInCountry`) is exactly
    this shape.
    """
    edge_vars = {p.var for p in node.pattern.edge_pats}
    out: dict[str, set] = {}
    for source in node.sources:
        if source.var in edge_vars:
            continue
        relation = sources.get(source.var)
        if relation is None:
            continue
        column = source.column or _sole_uid_column(relation)
        if column is None:
            continue
        out[source.var] = set(relation.column(column).tolist())
    return out


def _sole_uid_column(relation: Relation) -> str | None:
    columns = [c.name for c in relation.schema if c.tau.base == "uid"]
    return columns[0] if len(columns) == 1 else None


# ---------------------------------------------------------------------------
# domains
# ---------------------------------------------------------------------------

def _domain(node: PatternMatch, edge: EdgePat, sources: dict[str, Relation],
            adapter: Any) -> dict[str, np.ndarray]:
    """One edge variable's candidate set, as plain arrays.

    "The pushed and un-pushed forms must be semantically identical; only their
    cost estimates differ" (§2.9) — so a `sources` restriction narrows the
    domain and changes nothing else. §7.1 goes further and calls the pushdown
    "not an optimization but a *precondition for admission*", which is why an
    unrestricted variable falls back to a full typed scan rather than to a
    cross product.
    """
    bound = sources.get(edge.var)
    if bound is not None:
        return _columns_from(bound, edge)
    scan = EdgeScan("__e", rel_types=(edge.rel_type,) if edge.rel_type else None,
                    sigma_=node.sigma)
    rel = scan_nodes and scan  # keep the import honest for readers
    from tgms.tgir.eval.scan import scan_edges

    scanned = scan_edges(rel, adapter, frozenset(f"__e.{c}" for c in _KEY_COLUMNS))
    return {c: scanned.column(f"__e.{c}") for c in _KEY_COLUMNS}


def _columns_from(rel: Relation, edge: EdgePat) -> dict[str, np.ndarray]:
    """Pull the edge columns out of a `sources` relation, whatever variable
    prefix that relation happens to use — `sources` **rebinds**."""
    prefix = _edge_prefix(rel)
    out = {c: rel.column(f"{prefix}{c}") for c in _KEY_COLUMNS}
    if edge.rel_type is not None and f"{prefix}rel_type" in rel.schema:
        keep = rel.column(f"{prefix}rel_type") == edge.rel_type
        out = {k: v[keep] for k, v in out.items()}
    return out


def _edge_prefix(rel: Relation) -> str:
    for column in rel.schema:
        if column.name.endswith(".eid"):
            return column.name[: -len("eid")]
    raise AssertionError("a sources relation for an edge variable carries no eid")


def _search_order(node: PatternMatch,
                  domains: dict[str, dict[str, np.ndarray]]) -> list[EdgePat]:
    """Most-constrained-first: start from the smallest domain, then always take
    the pattern sharing the most already-bound node variables.

    That ordering is what keeps bo33's nine-edge motif tractable — the mutual
    3-clique closes before any of the three target edges opens, so the
    intermediate is triangles rather than the cross product of three stars.
    """
    remaining = list(node.pattern.edge_pats)
    remaining.sort(key=lambda e: len(domains[e.var]["eid"]))
    ordered: list[EdgePat] = [remaining.pop(0)]
    bound = {ordered[0].src, ordered[0].dst}
    while remaining:
        remaining.sort(key=lambda e: (-len({e.src, e.dst} & bound),
                                      len(domains[e.var]["eid"])))
        nxt = remaining.pop(0)
        ordered.append(nxt)
        bound |= {nxt.src, nxt.dst}
    return ordered


# ---------------------------------------------------------------------------
# the staged join
# ---------------------------------------------------------------------------

class _Bindings:
    """The running binding relation: one row per partial match.

    Held as parallel lists of per-variable arrays rather than as a `Relation`
    because the search binds *pattern variables*, not schema columns — the
    schema is assembled once at the end, in declaration order.
    """

    def __init__(self, node_domains: dict[str, set] | None = None) -> None:
        #: `sources` restrictions on node variables, applied as each one binds
        self.node_domains = node_domains or {}
        self.node_vars: list[str] = []
        self.edge_vars: list[str] = []
        self.nodes: dict[str, list[Any]] = {}
        self.edges: dict[str, dict[str, list[Any]]] = {}
        self.n = 0
        self._seeded = False

    def extend(self, edge: EdgePat, domain: dict[str, np.ndarray]) -> None:
        if not self._seeded:
            self._seed(edge, domain)
            self._restrict()
            return
        shared = [v for v in (edge.src, edge.dst) if v in self.nodes]
        index: dict[Any, list[int]] = defaultdict(list)
        size = len(domain["eid"])
        for i in range(size):
            key = self._domain_key(edge, domain, i, shared)
            index[key].append(i)

        left_idx: list[int] = []
        right_idx: list[int] = []
        for row in range(self.n):
            key = tuple(self.nodes[v][row] for v in shared)
            for i in index.get(key, ()):  # noqa: B007
                # edge-isomorphism over **identities**: no two edge variables
                # bind versions of one eid
                if domain["eid"][i] in self._bound_eids(row):
                    continue
                left_idx.append(row)
                right_idx.append(i)
        self._apply(edge, domain, left_idx, right_idx)
        self._restrict()

    def _restrict(self) -> None:
        """Drop rows whose bound node variables fall outside their `sources`
        cohort. Applied after every stage rather than once at the end, so a
        cohort prunes the search instead of only filtering its output."""
        if not self.node_domains:
            return
        keep = [i for i in range(self.n)
                if all(self.nodes[var][i] in allowed
                       for var, allowed in self.node_domains.items()
                       if var in self.nodes)]
        if len(keep) == self.n:
            return
        self.nodes = {var: [values[i] for i in keep]
                      for var, values in self.nodes.items()}
        self.edges = {var: {c: [values[c][i] for i in keep] for c in _KEY_COLUMNS}
                      for var, values in self.edges.items()}
        self.n = len(keep)

    def _seed(self, edge: EdgePat, domain: dict[str, np.ndarray]) -> None:
        size = len(domain["eid"])
        self.edge_vars.append(edge.var)
        self.edges[edge.var] = {c: list(domain[c]) for c in _KEY_COLUMNS}
        for var, column in ((edge.src, "src"), (edge.dst, "dst")):
            if var not in self.nodes:
                self.node_vars.append(var)
                self.nodes[var] = list(domain[column])
        self.n = size
        self._seeded = True

    def _domain_key(self, edge: EdgePat, domain: dict[str, np.ndarray], i: int,
                    shared: list[str]) -> tuple:
        out = []
        for var in shared:
            column = "src" if var == edge.src else "dst"
            out.append(domain[column][i])
        return tuple(out)

    def _bound_eids(self, row: int) -> set:
        return {self.edges[var]["eid"][row] for var in self.edge_vars}

    def _apply(self, edge: EdgePat, domain: dict[str, np.ndarray],
               left_idx: list[int], right_idx: list[int]) -> None:
        self.nodes = {var: [values[i] for i in left_idx]
                      for var, values in self.nodes.items()}
        self.edges = {var: {c: [values[c][i] for i in left_idx]
                            for c in _KEY_COLUMNS}
                      for var, values in self.edges.items()}
        self.edge_vars.append(edge.var)
        self.edges[edge.var] = {c: [domain[c][i] for i in right_idx]
                                for c in _KEY_COLUMNS}
        for var, column in ((edge.src, "src"), (edge.dst, "dst")):
            if var not in self.nodes:
                self.node_vars.append(var)
                self.nodes[var] = [domain[column][i] for i in right_idx]
        self.n = len(left_idx)


# ---------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------

def _materialize(node: PatternMatch, bindings: _Bindings, adapter: Any,
                 live: frozenset[str] | None, scans: Any = None) -> Relation:
    """Node-variable columns then edge-variable columns, **in pattern
    declaration order** (§4.2), then §2.9's canonical order.

    Node variables expose `(uid, vid, label, vt_s, vt_e, props)` — no `tt` pair
    — and their version columns come through `scan_nodes` under Σ, so a node
    with no visible version binds `uid` and nulls exactly as `Expand`'s `into`
    does.
    """
    order = _canonical_order(node, bindings)
    columns: list[Column] = []
    cols: dict[str, np.ndarray] = {}
    nulls: dict[str, np.ndarray] = {}

    for pat in node.pattern.node_pats:
        uids = np.array([bindings.nodes[pat.var][i] for i in order], dtype=object) \
            if bindings.n else np.array([], dtype=object)
        version = _versions(node, pat.var, adapter, uids, live, scans)
        for column in PATTERN_NODE_COLUMNS:
            name = f"{pat.var}.{column.name}"
            if live is not None and name not in live:
                continue
            columns.append(Column(name, column.tau))
            if column.name == "uid":
                cols[name] = uids
                continue
            values, missing = _gather(version, column.name, uids)
            cols[name] = values
            if missing.any():
                nulls[name] = missing

    for pat in node.pattern.edge_pats:
        for column in EDGE_COLUMNS:
            name = f"{pat.var}.{column.name}"
            if live is not None and name not in live:
                continue
            if column.name not in _KEY_COLUMNS:
                # the search carries only the columns it binds on; anything
                # else would be a second read per matched row
                continue
            columns.append(Column(name, column.tau))
            cols[name] = np.array(
                [bindings.edges[pat.var][column.name][i] for i in order],
                dtype=object)

    return Relation(Schema(tuple(columns)), cols, len(order), nulls)


def _canonical_order(node: PatternMatch, bindings: _Bindings) -> list[int]:
    """§2.9: "lexicographic over bound edge `(vt_s, vid)` in pattern declaration
    order, then bound node `uid` in declaration order"."""
    if not bindings.n:
        return []
    keys: list[tuple] = []
    for row in range(bindings.n):
        key: list[Any] = []
        for pat in node.pattern.edge_pats:
            edge = bindings.edges[pat.var]
            key.append((int(edge["vt_s"][row]), str(edge["vid"][row])))
        for pat in node.pattern.node_pats:
            key.append(str(bindings.nodes[pat.var][row]))
        keys.append(tuple(key))
    return sorted(range(bindings.n), key=lambda i: keys[i])


def _versions(node: PatternMatch, var: str, adapter: Any, uids: np.ndarray,
              live: frozenset[str] | None,
              scans: Any = None) -> dict[str, dict[str, Any]]:
    distinct = tuple(dict.fromkeys(str(u) for u in uids.tolist()))
    if not distinct:
        return {}
    wanted = frozenset(f"{var}.{c.name}" for c in PATTERN_NODE_COLUMNS
                       if live is None or f"{var}.{c.name}" in live)
    scan = NodeScan(var, uids=distinct, belief="current", vt_mode="overlap",
                    sigma_=node.sigma)
    rel = scan_nodes(scan, adapter, wanted | {f"{var}.uid"}, scans)
    out: dict[str, dict[str, Any]] = {}
    uid_col = rel.column(f"{var}.uid")
    for i in range(rel.n):
        uid = uid_col[i]
        if uid not in out:
            out[uid] = {name[len(var) + 1:]: rel.column(name)[i]
                        for name in rel.schema.names}
    return out


def _gather(version: dict[str, dict[str, Any]], column: str,
            uids: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    values: list[Any] = []
    missing = np.zeros(len(uids), dtype=bool)
    for i, uid in enumerate(uids.tolist()):
        row = version.get(uid)
        if row is None or column not in row:
            values.append(None)
            missing[i] = True
        else:
            values.append(row[column])
    return np.array(values, dtype=object), missing


def label_filter(node: PatternMatch, rel: Relation) -> Relation:
    """`(v : Label?)` — a node pattern's optional label, applied after the
    match. A node's label is a property of the *version* valid in Σ, which is
    what the bound version columns already carry."""
    keep = np.ones(rel.n, dtype=bool)
    for pat in node.pattern.node_pats:
        if pat.label is None:
            continue
        name = f"{pat.var}.label"
        if name not in rel.schema:
            continue
        keep &= (rel.column(name) == pat.label)
    return rel.filter(keep) if not keep.all() else rel


__all__ = ["eval_pattern", "label_filter"]
