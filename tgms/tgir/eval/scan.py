"""`NodeScan` and `EdgeScan` — the two store-reading evaluators (§2.1, §2.2).

Everything the store contributes to a plan enters here, so this is also where Σ
is applied (§3.3: exactly the four `reads_store` nodes, and no other node ever
sees an adapter).

**The frozen scan schemas name columns the columnar API cannot produce**, and
the coordinator's §9.8 ruling is the declared fallback rather than a new adapter
method or a Rust change:

| column family | route | price |
|---|---|---|
| `uid/vid/label/vt_s/vt_e` (node), `eid/vid/rel_type/vt_s/vt_e` (edge) | `{nodes,edges}_columnar` | the fast path |
| edge `src`/`dst` as uid **strings** | `uids_for` over the dense-id columns the scan returns | one gather per scan |
| edge `props` | `edges_columnar(columns=(…, "props"))` | opt-in, D-052's projection pushdown |
| node `props` | `props_for_vids("node", vids)` **after** every non-props predicate | by-vid, on survivors only |
| `tt_s`, `tt_e`, edge `disc` | `versions_columnar(kind)` — every version ever written | `version_history`'s measured **15,400 ms/M**, ~90× the columnar rate |
| `belief ∈ {superseded, all}` | `versions_columnar(kind)` + the §3.3 predicate | same |

A scan whose live columns fit the fast path takes it. One that does not takes
the fallback **and is priced at the fallback's coefficient**, so a large store
refuses rather than silently running two orders of magnitude slower.

**`labels` and `uids` are post-filters** (§9.3's ruling). `nodes_columnar` takes
only `as_of_tt`, `vt_min`, `vt_max` on all three backends — there is no label
predicate, no uid filter and no projection — so §2.1's "pushdown, not
post-filter" is *optimization intent*, not an observable. The semantics are
identical either way, and the filter lands here, marked at the site.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from tgms.core.errors import NotFoundError
from tgms.core.model import OPEN_END, clamp_tt
from tgms.temporal.props import parse_props
from tgms.tgir.node import EdgeScan, NodeScan
from tgms.tgir.relation import Relation
from tgms.tgir.types import Schema, Sigma

#: What `{nodes,edges}_columnar` can produce, keyed by the *unprefixed* schema
#: column. Anything a plan reads outside this set forces the `versions_columnar`
#: fallback — which is exactly the check `needs_fallback` performs.
NODE_FAST_COLUMNS: frozenset[str] = frozenset({"uid", "vid", "label", "vt_s", "vt_e",
                                               "props"})
EDGE_FAST_COLUMNS: frozenset[str] = frozenset({"eid", "vid", "src", "dst", "rel_type",
                                               "vt_s", "vt_e", "props"})


def scan_nodes(node: NodeScan, adapter: Any, live: frozenset[str] | None) -> Relation:
    """§2.1. Emits one row per believed node **version**, not per entity: under
    an instant scope that is at most one row per uid, under a window scope it is
    every version overlapping the window — which is what makes `entity_history`
    and `version_history` core-expressible."""
    wanted = _wanted(node, live)
    fallback = needs_fallback(node, wanted, NODE_FAST_COLUMNS)
    if fallback:
        cols = _versions_fallback(adapter, "node", node.belief, node.sigma)
    else:
        cols = _nodes_fast(adapter, node.sigma)

    keep = _sigma_mask(cols, node.sigma, node.vt_mode)
    if node.labels is not None:
        # §9.3: a post-filter, because no backend has a label predicate
        keep &= np.isin(cols["label"], np.array(node.labels, dtype=object))
    if node.uids is not None:
        keep &= np.isin(cols["uid"], np.array(node.uids, dtype=object))
    cols = {k: v[keep] for k, v in cols.items()}
    cols = _canonical_order(cols, node.belief, fallback)
    return _assemble(node.as_, node.out_schema, cols, wanted, adapter, "node",
                     node.sigma)


def scan_edges(node: EdgeScan, adapter: Any, live: frozenset[str] | None) -> Relation:
    """§2.2. `endpoints` is an incidence pushdown and **the only cohort
    restriction in the algebra that is not a `Join`** — it selects each matching
    edge version exactly once, where a join against a uid list would multiply
    rows whose two endpoints are both in the cohort."""
    wanted = _wanted(node, live)
    fallback = needs_fallback(node, wanted, EDGE_FAST_COLUMNS)
    if fallback:
        cols = _versions_fallback(adapter, "edge", node.belief, node.sigma)
        if node.rel_types is not None:
            keep = np.isin(cols["rel_type"], np.array(node.rel_types, dtype=object))
            cols = {k: v[keep] for k, v in cols.items()}
        if node.endpoints is not None:
            cols = _endpoint_filter_by_uid(cols, node)
    else:
        cols = _edges_fast(adapter, node, wanted)

    keep = _sigma_mask(cols, node.sigma, node.vt_mode)
    cols = {k: v[keep] for k, v in cols.items()}
    cols = _canonical_order(cols, node.belief, fallback)
    return _assemble(node.as_, node.out_schema, cols, wanted, adapter, "edge",
                     node.sigma)


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------

def _wanted(node: NodeScan | EdgeScan, live: frozenset[str] | None) -> frozenset[str]:
    """The unprefixed columns this scan must materialize.

    `live` is the plan-time pruning result (§3.1): a scan builds exactly the
    columns some node downstream reads. With none it builds its whole declared
    schema, which is what a bare `evaluate(node)` outside a plan wants.
    """
    prefix = f"{node.as_}."
    declared = {c.name[len(prefix):] for c in node.out_schema}
    if live is None:
        return frozenset(declared)
    return frozenset(name[len(prefix):] for name in live
                     if name.startswith(prefix)) & frozenset(declared)


def needs_fallback(node: NodeScan | EdgeScan, wanted: frozenset[str],
                   fast: frozenset[str]) -> bool:
    """True when this scan must read `versions_columnar`.

    Two reasons, both structural: a live column the fast route does not carry
    (`tt_s`, `tt_e`, edge `disc`), or a belief mode the columnar route cannot
    express — `{nodes,edges}_columnar` apply `tt_s ≤ as_of < tt_e` inside the
    backend, which is `current` and nothing else.
    """
    return node.belief != "current" or bool(wanted - fast)


def _nodes_fast(adapter: Any, sigma: Sigma) -> dict[str, np.ndarray]:
    hull = sigma.hull
    cols = adapter.nodes_columnar(as_of_tt=sigma.t_b, vt_min=hull.start,
                                  vt_max=hull.end)
    return {"uid": cols["uid"], "vid": cols["vid"], "label": cols["label"],
            "vt_s": cols["vt_s"], "vt_e": cols["vt_e"]}


def _edges_fast(adapter: Any, node: EdgeScan,
                wanted: frozenset[str]) -> dict[str, np.ndarray]:
    hull = node.sigma.hull
    columns = ["src_id", "dst_id", "vt_s", "vt_e", "eid", "vid", "rel_type"]
    if "props" in wanted:
        columns.append("props")
    touching, both = _touching(adapter, node)
    cols = adapter.edges_columnar(
        as_of_tt=node.sigma.t_b, vt_min=hull.start, vt_max=hull.end,
        rel_types=list(node.rel_types) if node.rel_types else None,
        columns=tuple(columns), touching_ids=touching, touching_both=both)
    out = {"eid": cols["eid"], "vid": cols["vid"], "rel_type": cols["rel_type"],
           "vt_s": cols["vt_s"], "vt_e": cols["vt_e"],
           "src_id": cols["src_id"], "dst_id": cols["dst_id"]}
    if "props" in wanted:
        out["props"] = cols["props"]
    if node.endpoints is not None and node.endpoints.role in ("src", "dst"):
        # `touching_ids` is an OR over both endpoints, so a single-sided role
        # needs the numpy post-mask the plan's §3.2 table calls for
        ids = np.array(_dense(adapter, node.endpoints.uids), dtype=np.int64)
        side = out["src_id"] if node.endpoints.role == "src" else out["dst_id"]
        keep = np.isin(side, ids)
        out = {k: v[keep] for k, v in out.items()}
    return out


def _touching(adapter: Any, node: EdgeScan) -> tuple[list[int] | None, bool]:
    if node.endpoints is None:
        return None, False
    ids = _dense(adapter, node.endpoints.uids)
    return ids, node.endpoints.role == "both"


def _dense(adapter: Any, uids: tuple[str, ...]) -> list[int]:
    """Dense ids for a bind-time uid set, **skipping uids the store has never
    seen**.

    `dense_ids` raises `E_NOT_FOUND` on a miss, which is the *leaf* operators'
    outcome boundary. A core scan has none: FRESHNESS_SEMANTICS D13.15 (RG-10)
    grants `Expand`'s `exact(0)` an exemption from L13.3's incident arm on
    exactly the grounds that "a core scan has no unknown-uid outcome boundary —
    it has no `E_NOT_FOUND` to flip", and that exemption is only sound if the
    scan really does not raise. An unknown uid simply has no rows.
    """
    out: list[int] = []
    for uid in uids:
        try:
            out.extend(int(i) for i in adapter.dense_ids([uid]))
        except NotFoundError:
            continue
    return out


def _endpoint_filter_by_uid(cols: dict[str, np.ndarray],
                            node: EdgeScan) -> dict[str, np.ndarray]:
    """The incidence filter on the fallback path, where endpoints are uid
    strings rather than dense ids."""
    assert node.endpoints is not None
    uids = np.array(node.endpoints.uids, dtype=object)
    in_src = np.isin(cols["src"], uids)
    in_dst = np.isin(cols["dst"], uids)
    role = node.endpoints.role
    keep = {"src": in_src, "dst": in_dst, "either": in_src | in_dst,
            "both": in_src & in_dst}[role]
    return {k: v[keep] for k, v in cols.items()}


def _versions_fallback(adapter: Any, kind: str, belief: str,
                       sigma: Sigma) -> dict[str, np.ndarray]:
    """Every version ever written, then §3.3's belief predicate in numpy.

    Priced at `version_history`'s measured 15,400 ms/M — this reads the whole
    version log, including versions no longer believed, because that is what
    `belief ∈ {superseded, all}` *means* and no columnar route expresses it.
    """
    cols = dict(adapter.versions_columnar(kind))
    at = clamp_tt(sigma.t_b)
    tt_s, tt_e = cols["tt_s"], cols["tt_e"]
    if belief == "current":
        keep = (tt_s <= at) & (at < tt_e)
    elif belief == "superseded":
        keep = (tt_s <= at) & (tt_e <= at)
    else:  # "all" — everything written by T_b
        keep = tt_s <= at
    cols = {k: v[keep] for k, v in cols.items()}
    # §3.4's censoring rule, applied wherever `tt_e` is materialized: a belief
    # that ended after T_b had not ended yet. Without it a pinned result leaks
    # knowledge the caller's belief state does not have and §3.6 fails.
    cols["tt_e"] = np.where(cols["tt_e"] > at, OPEN_END, cols["tt_e"])
    return cols


def _sigma_mask(cols: dict[str, np.ndarray], sigma: Sigma, vt_mode: str) -> np.ndarray:
    """§3.2's three keying modes, over Σ's (possibly several) intervals.

    The adapter's only valid-time predicate is *overlap* against one hull, so
    the exact mode is applied here — which also means a Σ carrying two disjoint
    instants (`diff_snapshots`' shape) is exact rather than hulled.
    """
    vt_s, vt_e = cols["vt_s"], cols["vt_e"]
    keep = np.zeros(len(vt_s), dtype=bool)
    for interval in sigma.t_v:
        a, b = interval.start, interval.end
        if vt_mode == "instant":
            keep |= (vt_s <= a) & (a < vt_e)
        elif vt_mode == "event":
            keep |= (a <= vt_s) & (vt_s < b)
        else:  # overlap
            keep |= (vt_s < b) & (a < vt_e)
    return keep


def _canonical_order(cols: dict[str, np.ndarray], belief: str,
                     fallback: bool) -> dict[str, np.ndarray]:
    """§2.1/§2.2's declared order: `(vt_s, vid)`, or `(tt_s, vid)` under a
    non-`current` belief mode — "the belief log's own order", which is the
    difference between a version scan and a snapshot.

    The fast path is **already** in `(vt_s, vid)`: both backends order their
    columnar scans that way (`duckdb_adapter.py:265,285`; `lib.rs:796`). The
    ABC's docstring understates it as "Sorted by vt_s", so the guarantee is
    re-asserted by `tests/test_tgir_eval_core.py` rather than assumed — a
    backend that stopped tie-breaking on `vid` would fail there rather than
    silently reordering a plan's output.

    `versions_columnar` promises **no order at all**, so the fallback always
    sorts. Skipping that was a real defect: it made a pruned and an unpruned
    execution of the same plan return the same rows in different orders,
    depending only on which columns a consumer happened to read.
    """
    if belief != "current":
        return _lexsort(cols, ("vid", "tt_s"))
    if fallback:
        return _lexsort(cols, ("vid", "vt_s"))
    return cols


def _lexsort(cols: dict[str, np.ndarray], keys: tuple[str, ...]) -> dict[str, np.ndarray]:
    """`np.lexsort` orders by the **last** key first, so `keys` is written
    minor-to-major: `("vid", "tt_s")` sorts by `(tt_s, vid)`."""
    if not cols or len(next(iter(cols.values()))) == 0:
        return cols
    order = np.lexsort(tuple(cols[k] for k in keys))
    return {k: v[order] for k, v in cols.items()}


def _assemble(var: str, schema: Schema, cols: dict[str, np.ndarray],
              wanted: frozenset[str], adapter: Any, kind: str,
              sigma: Sigma) -> Relation:
    """Prefix the columns, resolve the two derived families, and drop everything
    the plan does not read."""
    n = len(next(iter(cols.values()))) if cols else 0

    if "src" in wanted and "src" not in cols and "src_id" in cols:
        cols["src"] = _uids(adapter, cols["src_id"])
    if "dst" in wanted and "dst" not in cols and "dst_id" in cols:
        cols["dst"] = _uids(adapter, cols["dst_id"])
    if "props" in wanted and "props" not in cols:
        # node props have no columnar route at all: fetch by vid, on the rows
        # that survived every predicate that does not itself read props
        bags = adapter.props_for_vids(kind, [str(v) for v in cols["vid"]])
        cols["props"] = np.array([parse_props(bags.get(str(v), {}))
                                  for v in cols["vid"]], dtype=object)
    elif "props" in cols:
        cols["props"] = np.array([parse_props(p) for p in cols["props"]], dtype=object)
    if "tt_s" not in cols and "tt_s" in wanted:  # pragma: no cover - routed above
        raise AssertionError("tt columns require the versions fallback")

    keep = Schema(tuple(c for c in schema if c.name[len(var) + 1:] in wanted))
    out = {c.name: cols[c.name[len(var) + 1:]] for c in keep}
    return Relation(keep, out, n, {})


def _uids(adapter: Any, ids: np.ndarray) -> np.ndarray:
    if len(ids) == 0:
        return np.array([], dtype=object)
    return np.array(adapter.uids_for([int(i) for i in ids]), dtype=object)


__all__ = ["EDGE_FAST_COLUMNS", "NODE_FAST_COLUMNS", "needs_fallback", "scan_edges",
           "scan_nodes"]
