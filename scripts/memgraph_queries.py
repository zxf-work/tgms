"""Registry queries for Memgraph — the Neo4j Cypher, with one override.

Twelve of the thirteen registry queries run unchanged against Memgraph
through the same bolt driver; openCypher compatibility is the entire point
of reusing them, and the canonical hash judges the reuse rather than
trusting it. The one override: `neighborhood_evolution`'s degree series used Neo4j
5's scoped `CALL (bs) {...}` subquery, which Memgraph does not parse — here
it is a correlated UNWIND/MATCH with empty buckets refilled client-side.
"""

from __future__ import annotations

from typing import Any

from neo4j_queries import (  # noqa: F401  (re-exported registry entries)
    OPEN_END,
    _page,
    aggregate_events,
    belief,
    burst_detection,
    co_active,
    count_temporal_motifs,
    diff_snapshots,
    entity_history,
    graph_metric_timeseries,
    resolve_entities,
    snapshot_subgraph,
    temporal_paths,
    temporal_reachability,
)


def neighborhood_evolution(drv, *, uid: str, t1: int, t2: int,
                           stride: int | None = None, as_of_tt: int = OPEN_END,
                           limit: int = 100) -> dict[str, Any]:
    stride = stride or max(1, (t2 - t1) // 20)
    rbel = belief(as_of_tt, "r")
    with drv.session() as s:
        def nbrs(t: int) -> set[str]:
            rows = s.run(
                f"MATCH (e:Entity {{uid: $uid}})-[r:E]-(m:Entity) "
                f"WHERE {rbel} AND r.vt_s <= $t AND $t < r.vt_e "
                f"AND m.uid <> $uid RETURN DISTINCT m.uid AS u",
                uid=uid, t=t).data()
            return {r["u"] for r in rows}

        m1, m2 = nbrs(t1), nbrs(t2)
        got = {int(r["bs"]): int(r["deg"]) for r in s.run(
            f"UNWIND range($t1, $t2 - 1, $stride) AS bs "
            f"MATCH (e:Entity {{uid: $uid}})-[r:E]-() "
            f"WHERE {rbel} AND r.vt_s <= bs AND r.vt_e > bs "
            f"RETURN bs, count(r) AS deg",
            uid=uid, t1=t1, t2=t2, stride=stride).data()}
    gained, lost = sorted(m2 - m1), sorted(m1 - m2)
    return {
        "neighbors_gained": gained[:limit], "neighbors_gained_total": len(gained),
        "neighbors_lost": lost[:limit], "neighbors_lost_total": len(lost),
        "degree_series": [{"t": bs, "degree": got.get(bs, 0)}
                          for bs in range(t1, t2, stride)],
        "stride": stride,
        "truncated": len(gained) > limit or len(lost) > limit,
    }


QUERIES = {
    "hist.single": entity_history,
    "hist.asof": entity_history,
    "snap.hop2": snapshot_subgraph,
    "reach.window": temporal_reachability,
    "paths.k": temporal_paths,
    "series.count": graph_metric_timeseries,
    "burst.zscore": burst_detection,
    "agg.rel_bucket": aggregate_events,
    "nbr.evolution": neighborhood_evolution,
    "diff.global": diff_snapshots,
    "coactive.narrow": co_active,
    "resolve.substr": resolve_entities,
    "motif.filtered": count_temporal_motifs,
}
