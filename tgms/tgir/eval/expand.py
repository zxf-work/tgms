"""`Expand` — all three hop forms of §2.3 (R6).

**The two families are different relations and the spec keeps them apart**
(§2.3, adjudication §8.7):

- **`exact(k)` is a walk relation** — the k-fold relational composition of the
  one-hop step, multiplicity-preserving and edge-bindable. Two distinct k-walks
  to the same target are two rows.
- **`bounded(a,b)` and `unbounded(a)` are node relations** — the union over
  `j ∈ [a,b]` of `exact(j)`, **deduplicated by `(input row, into)` keeping the
  minimum `j`**, which is reported in `<into>.depth`.

`bounded(k,k)` is therefore **not** normalized to `exact(k)`, and this module
must not: they differ whenever a target is reachable by several k-walks, and
that difference is proved on a concrete store in `tests/test_tgir_eval_expand.py`.

Three restrictions, each load-bearing and each tested directly:

1. **No edge bindings for variable-length forms.** A variable number of edges
   cannot bind into a fixed row schema without list values, and v1 has no list
   type — which is precisely why the path family stays outside the core. The
   node layer rejects it at construction.
2. **Structural closure only — no time-respecting expansion.** Every hop is
   evaluated under one Σ, and there is no constraint that hop *i+1*'s `vt_s`
   exceed hop *i*'s. R6's unbounded `Expand` is therefore **not** the
   reachability operator: `temporal_reachability` additionally imposes the
   ordering constraint and an earliest-arrival semiring, and stays an opaque
   leaf.
3. **An unbounded `Expand` is never truncated.** Its outcome is `complete` or
   `Refused` — a partial fixpoint produces *false absences*, and the evidence
   contract permits false invalidation but never false certification.

**Coordinator ruling (§9.1), applied at `_into_columns` below: an `into` whose
node has no version visible under Σ binds `uid` from the edge endpoint and
**null** in every version column.** §6 #3 rejects making node validity a global
`Expand` rule ("it would silently drop edges to version-less nodes for every
other row in the corpus") and requires the compiled `snapshot_subgraph` to
interpose an explicit `Join{inner}` against `NodeScan @ instant($t)` instead —
which is expressible only if the version-less row survives the expansion to be
joined away.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from tgms.core.errors import NotFoundError
from tgms.tgir.eval.adjacency import AdjacencyCache
from tgms.tgir.eval.scan import scan_nodes
from tgms.tgir.node import Bounded, Exact, Expand, NodeScan, Unbounded
from tgms.tgir.relation import Relation
from tgms.tgir.types import EDGE_COLUMNS, NODE_COLUMNS, Column, Schema, T_INT


def eval_expand(node: Expand, rel: Relation, adapter: Any,
                cache: AdjacencyCache, live: frozenset[str] | None = None,
                budget: Any = None) -> Relation:
    """One `Expand`, over an already-evaluated input relation."""
    from_uids = rel.column(node.from_column)
    dense = _dense_map(adapter, from_uids)
    # an `edge_var` binding needs an index carrying `vid`; a pure node-set
    # expansion can share the cached CSR, which is exactly `ops_paths`' use
    need_identity = node.edge_var is not None
    adjacency = cache.get(node.rel_type, node.sigma, need_identity=need_identity)

    if isinstance(node.hops, Exact):
        rows, targets, edge_rows = _walks(node, rel, dense, adjacency, budget)
        depths = None
    else:
        rows, targets, depths = _node_set(node, rel, dense, adjacency, budget)
        edge_rows = None

    out = rel.take(np.asarray(rows, dtype=np.int64))
    out = _with_into(out, node, targets, adapter, live)
    if edge_rows is not None and node.edge_var is not None:
        out = _with_edge(out, node, adjacency, np.asarray(edge_rows, dtype=np.int64),
                         adapter, live)
    if depths is not None:
        out = out.with_columns(
            Schema.of(Column(f"{node.into}.depth", T_INT)),
            {f"{node.into}.depth": np.asarray(depths, dtype=np.int64)})
    return out


# ---------------------------------------------------------------------------
# the two families
# ---------------------------------------------------------------------------

def _walks(node: Expand, rel: Relation, dense: dict[str, int],
           adjacency: Any, budget: Any) -> tuple[list, list, list]:
    """`exact(k)`: the k-fold composition, multiplicity preserved.

    Order is input row position, then the traversal's own order — each hop's
    slices arrive in `(vt_s, vid)` and the chain is walked depth-first in that
    order, so the composition is lexicographic over the hops. §2.3's canonical
    order names the traversal's `(vt_s, vid)` without saying which hop's for
    `k > 1`; the lexicographic reading is the one a chain of stable joins
    produces, and it is recorded rather than assumed.
    """
    k = node.hops.k          # type: ignore[union-attr]
    from_uids = rel.column(node.from_column)

    rows: list[int] = []
    targets: list[int | None] = []
    edge_rows: list[int] = []
    for i in range(rel.n):
        seed = dense.get(from_uids[i])
        if seed is None:
            continue
        if k == 0:
            # traverses no edge at all: `into` *is* `from`
            rows.append(i)
            targets.append(seed)
            edge_rows.append(-1)
            continue
        frontier: list[tuple[int, int]] = [(seed, -1)]
        for _hop in range(k):
            nxt: list[tuple[int, int]] = []
            for current, _last in frontier:
                nbr, row = adjacency.step(int(current), node.dir)
                _charge(budget, len(nbr))
                nxt.extend(zip(nbr.tolist(), row.tolist()))
            frontier = nxt
            if not frontier:
                break
        for target, edge_row in frontier:
            rows.append(i)
            targets.append(int(target))
            edge_rows.append(int(edge_row))
    return rows, targets, edge_rows


def _node_set(node: Expand, rel: Relation, dense: dict[str, int],
              adjacency: Any, budget: Any) -> tuple[list, list, list]:
    """`bounded(a,b)` / `unbounded(a)`: a node relation, deduplicated by
    `(input row, into)` at **minimum depth**.

    The dedup is what makes `bounded(k,k)` differ from `exact(k)`, and the
    minimum is taken **within `[a, b]`** — which is why BI10's far-minus-near
    shape needs a `Join{anti}` of two expansions rather than one banded
    expansion: `bounded(a,b)` admits a node whose true minimum distance is
    below `a`.

    The fixpoint is complete or refused, never truncated: the only way out
    other than exhaustion is the budget, which raises.
    """
    spec = node.hops
    lo = spec.a                                    # type: ignore[union-attr]
    hi = spec.b if isinstance(spec, Bounded) else None
    from_uids = rel.column(node.from_column)

    rows: list[int] = []
    targets: list[int] = []
    depths: list[int] = []
    for i in range(rel.n):
        seed = dense.get(from_uids[i])
        if seed is None:
            continue
        seen: dict[int, int] = {int(seed): 0}
        frontier = [int(seed)]
        depth = 0
        while frontier and (hi is None or depth < hi):
            depth += 1
            nxt: list[int] = []
            for current in frontier:
                nbr, _row = adjacency.step(current, node.dir)
                _charge(budget, len(nbr))
                for target in nbr.tolist():
                    if int(target) not in seen:
                        seen[int(target)] = depth
                        nxt.append(int(target))
            frontier = nxt
        for target, at in seen.items():
            if at < lo:
                continue
            rows.append(i)
            targets.append(target)
            depths.append(at)
    return _order_node_set(rows, targets, depths)


def _order_node_set(rows: list[int], targets: list[int],
                    depths: list[int]) -> tuple[list, list, list]:
    """§2.3's variable-length canonical order: `(input row position,
    into.depth, into)`."""
    order = sorted(range(len(rows)), key=lambda i: (rows[i], depths[i], targets[i]))
    return ([rows[i] for i in order], [targets[i] for i in order],
            [depths[i] for i in order])


# ---------------------------------------------------------------------------
# binding the columns
# ---------------------------------------------------------------------------

def _with_into(rel: Relation, node: Expand, targets: list[int | None],
               adapter: Any, live: frozenset[str] | None) -> Relation:
    """Bind `into`'s node columns, with §9.1's nulls where no version is
    visible under Σ.

    The version lookup reuses `scan_nodes` over a synthetic `NodeScan` rather
    than reading the store a second way — so `into`'s columns come through the
    same Σ predicate, the same censoring rule, the same fallback routing and
    the same pruning as any other scan in the plan.
    """
    uids = np.array(adapter.uids_for([int(t) for t in targets]) if targets else [],
                    dtype=object)
    wanted = _into_columns(node, live)
    version = _versions_by_uid(node, adapter, uids, wanted)

    cols: dict[str, np.ndarray] = {}
    nulls: dict[str, np.ndarray] = {}
    columns: list[Column] = []
    for column in NODE_COLUMNS:
        name = f"{node.into}.{column.name}"
        if name not in wanted:
            continue
        columns.append(Column(name, column.tau))
        if column.name == "uid":
            # **Coordinator ruling (§9.1): the uid always populates.** It comes
            # from the edge endpoint, which exists whether or not the node has
            # a version visible under Σ — and it is what a downstream
            # `Join{inner}` against `NodeScan @ instant($t)` joins on when a
            # plan does want node validity enforced (§6 #3's prescribed shape).
            cols[name] = uids
            continue
        values, missing = _gather(version, column.name, uids)
        cols[name] = values
        if missing.any():
            # ... and every *version* column goes null for a node with no
            # version visible under Σ. §6 #3 rejected making node validity a
            # global Expand rule, so this row must survive to be joined away.
            nulls[name] = missing
    return rel.with_columns(Schema(tuple(columns)), cols, nulls)


def _into_columns(node: Expand, live: frozenset[str] | None) -> frozenset[str]:
    prefix = f"{node.into}."
    declared = {f"{prefix}{c.name}" for c in NODE_COLUMNS}
    if live is None:
        return frozenset(declared)
    # `uid` is always materialized: it is the identity the row is *about*, and
    # every downstream shape that treats a version-less node as absent joins on
    # it
    return frozenset(declared & (set(live) | {f"{prefix}uid"}))


def _versions_by_uid(node: Expand, adapter: Any, uids: np.ndarray,
                     wanted: frozenset[str]) -> dict[str, dict[str, Any]]:
    """The Σ-visible node version per uid, as `{uid: {column: value}}`.

    A uid with **several** visible versions (a window Σ, where one identity can
    have many) keeps the **first in canonical order** `(vt_s, vid)` — one row
    per walk, not one per version, since §2.3 binds `into.(node-columns)` and
    not a version cross-product. §2.3 does not state which version that is; the
    choice is recorded here rather than left to the scan's incidental order.
    """
    distinct = tuple(dict.fromkeys(str(u) for u in uids.tolist()))
    if not distinct:
        return {}
    scan = NodeScan(node.into, uids=distinct, belief="current",
                    vt_mode="overlap", sigma_=node.sigma)
    rel = scan_nodes(scan, adapter, frozenset(wanted))
    out: dict[str, dict[str, Any]] = {}
    uid_col = rel.column(f"{node.into}.uid")
    for i in range(rel.n):
        uid = uid_col[i]
        if uid in out:
            continue                       # first in canonical order wins
        out[uid] = {name[len(node.into) + 1:]: rel.column(name)[i]
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


def _with_edge(rel: Relation, node: Expand, adjacency: Any, edge_rows: np.ndarray,
               adapter: Any, live: frozenset[str] | None) -> Relation:
    """Bind `edge_var`'s columns from the traversed edge.

    **Only the final hop's edge binds.** §2.3 says `exact(k)` with `k > 1`
    "binds `edge_var₁ … edge_vₖ` if requested", but `node.py`'s `Expand`
    carries a single `edge_var` and §4.2 concatenates one edge schema — so k
    prefixed edge variables are not expressible in the frozen node type, and
    the plan is explicit that a node needing a field it does not carry is a
    design signal rather than a licence to add one. Recorded for adjudication.
    """
    cols: dict[str, np.ndarray] = {}
    nulls: dict[str, np.ndarray] = {}
    columns: list[Column] = []
    source = adjacency.cols
    valid = edge_rows >= 0
    prefix = f"{node.edge_var}."
    by_vid = _VersionLookup(adapter, source, edge_rows, valid)
    for column in EDGE_COLUMNS:
        name = f"{prefix}{column.name}"
        if live is not None and name not in live:
            continue
        columns.append(Column(name, column.tau))
        cols[name], missing = _edge_values(source, column.name, edge_rows, valid,
                                           adapter, by_vid)
        if missing.any():
            nulls[name] = missing
    return rel.with_columns(Schema(tuple(columns)), cols, nulls)


def _edge_values(source: dict[str, np.ndarray], column: str, rows: np.ndarray,
                 valid: np.ndarray, adapter: Any,
                 by_vid: "_VersionLookup") -> tuple[np.ndarray, np.ndarray]:
    """One edge column at the traversed rows.

    The CSR is built from `TCSR_COLS`, and the native backend redefines that to
    swap `eid`/`rel_type` out for physical row addresses — so the index carries
    only part of §2.2's edge schema. A column it does not carry (`disc`,
    `tt_s`, `tt_e`, `props`) is **not** nulled, which would misreport a column
    the store holds: it takes the same declared fallback the scans take
    (`versions_columnar`, by `vid`), priced at the same 15,400 ms/M. Only a row
    that traversed no edge at all — `exact(0)` — is null here.
    """
    missing = ~valid
    if column in ("src", "dst") and f"{column}_id" in source:
        ids = source[f"{column}_id"]
        picked = [int(ids[r]) if valid[i] else 0 for i, r in enumerate(rows)]
        uids = adapter.uids_for(picked) if picked else []
        return np.array([u if valid[i] else None for i, u in enumerate(uids)],
                        dtype=object), missing
    if column in source:
        values = source[column]
        return (np.array([values[r] if valid[i] else None
                          for i, r in enumerate(rows)], dtype=object), missing)
    return by_vid.gather(column)


class _VersionLookup:
    """The `versions_columnar` fallback for edge columns the adjacency index
    does not carry — built **once per expansion and only if asked**, since it
    reads every edge version ever written."""

    def __init__(self, adapter: Any, source: dict[str, np.ndarray],
                 rows: np.ndarray, valid: np.ndarray) -> None:
        self.adapter = adapter
        self.source = source
        self.rows = rows
        self.valid = valid
        self._by_vid: dict[str, dict[str, Any]] | None = None

    def _props(self) -> tuple[np.ndarray, np.ndarray]:
        """Edge `props` come from `props_for_vids`, not from the version table:
        `VERSION_COLS` deliberately excludes the blob (D-058 refused it on a
        whole-store scan), so the by-vid route is the only one that carries it —
        the same route node `props` take in the scans."""
        from tgms.temporal.props import parse_props

        vids = self.source.get("vid")
        wanted = [str(vids[r]) for i, r in enumerate(self.rows.tolist())
                  if self.valid[i] and vids is not None]
        bags = self.adapter.props_for_vids("edge", list(dict.fromkeys(wanted))) \
            if wanted else {}
        values: list[Any] = []
        missing = np.zeros(len(self.rows), dtype=bool)
        for i, row in enumerate(self.rows.tolist()):
            if not self.valid[i] or vids is None:
                values.append(None)
                missing[i] = True
            else:
                values.append(parse_props(bags.get(str(vids[row]), {})))
        return np.array(values, dtype=object), missing

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._by_vid is None:
            cols = self.adapter.versions_columnar("edge")
            names = list(cols)
            self._by_vid = {
                str(cols["vid"][i]): {n: cols[n][i] for n in names}
                for i in range(len(cols["vid"]))
            }
        return self._by_vid

    def gather(self, column: str) -> tuple[np.ndarray, np.ndarray]:
        if column == "props":
            return self._props()
        table = self._load()
        vids = self.source.get("vid")
        values: list[Any] = []
        missing = np.zeros(len(self.rows), dtype=bool)
        for i, row in enumerate(self.rows.tolist()):
            record = None if (not self.valid[i] or vids is None) else \
                table.get(str(vids[row]))
            if record is None or column not in record:
                values.append(None)
                missing[i] = True
            else:
                values.append(record[column])
        return np.array(values, dtype=object), missing


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _dense_map(adapter: Any, uids: np.ndarray) -> dict[str, int]:
    """uid → dense id, skipping uids the store has never seen — a core scan has
    no `E_NOT_FOUND` (D13.15, RG-10), and an expansion from an unknown seed
    simply has no neighbours."""
    out: dict[str, int] = {}
    for uid in dict.fromkeys(uids.tolist()):
        try:
            out[uid] = int(adapter.dense_ids([uid])[0])
        except NotFoundError:
            continue
    return out


def _charge(budget: Any, expansions: int) -> None:
    if budget is not None:
        budget.charge(expansions)


def hop_bounds(node: Expand) -> tuple[int, int | None]:
    """`(a, b)` for any hop form; `b is None` for the unbounded one."""
    spec = node.hops
    if isinstance(spec, Exact):
        return spec.k, spec.k
    if isinstance(spec, Bounded):
        return spec.a, spec.b
    assert isinstance(spec, Unbounded)
    return spec.a, None


__all__ = ["eval_expand", "hop_bounds"]
