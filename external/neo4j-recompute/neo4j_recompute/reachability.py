"""F11 `temporal_reachability`: client-driven iterated Cypher (design memo
§2.3). No single-statement form exists in Neo4j 5.26 — quantified path
patterns cannot carry an accumulating arrival time across hops, and Cypher
25's `allReduce` (which could) is not in 5.26.

Protocol, matching `tgms.temporal.ops_paths.temporal_reachability`'s own
vectorized label-correcting fixpoint exactly (no `delta_max_wait`, so
prefix-optimality holds and frontier-only relaxation is sound, not an
approximation): `best[src] = t_a`; each round sends the frontier of nodes
whose label just improved; a round's query relaxes every outgoing edge from
that frontier once; any `(uid, arr)` strictly better than `best.get(uid)` is
recorded and re-queued for the next round; stop when a round improves
nothing. Earliest arrival is exact here precisely because a later arrival
can never disable an earlier one (true only without `delta_max_wait` — see
`queries.py::f11_window_params`, which refuses any other case rather than
mis-answering it).
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .queries import F11_REACHABILITY_ROUND

#: Safety valve: a round count past this on a finite, acyclic-in-time graph
#: would mean the fixpoint is not converging — a defect to report, not to
#: loop on forever. (Earliest-arrival labels only ever improve and are
#: bounded by t_b - t_a many *distinct* integer values along any one path,
#: but pathological fan-out could still need many rounds; this is a guard,
#: not a tuned cap.)
MAX_ROUNDS = 100_000


def run_reachability(run_query: Callable[[str, dict[str, Any]], list[dict]],
                     src: str, t_a: int, t_b: int) -> list[dict[str, Any]]:
    """Drive the fixpoint; `run_query(cypher, params) -> list of row dicts`
    is the caller's Bolt session wrapper (`runner.py`), so this function
    stays transport-agnostic and unit-testable with a fake.

    Returns `best`, sorted by (arrival, uid), excluding `src` — the same
    shape `temporal_reachability`'s Python implementation builds just
    before calling `paginate`.
    """
    best: dict[str, int] = {src: t_a}
    frontier = [{"uid": src, "arr": t_a}]
    rounds = 0
    while frontier:
        rounds += 1
        if rounds > MAX_ROUNDS:
            raise RuntimeError(
                f"temporal_reachability: fixpoint did not converge in {MAX_ROUNDS} rounds")
        rows = run_query(F11_REACHABILITY_ROUND,
                         {"front": frontier, "t_a": t_a, "t_b": t_b, "O": _O()})
        next_frontier: list[dict[str, Any]] = []
        for row in rows:
            uid, arr = row["uid"], row["arr"]
            if uid not in best or arr < best[uid]:
                best[uid] = arr
                next_frontier.append({"uid": uid, "arr": arr})
        frontier = next_frontier
    rows_out = [{"uid": u, "earliest_arrival": a} for u, a in best.items() if u != src]
    rows_out.sort(key=lambda r: (r["earliest_arrival"], r["uid"]))
    return rows_out


def _O() -> int:
    from .canon import OPEN_END
    return OPEN_END
