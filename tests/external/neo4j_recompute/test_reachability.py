"""F11's client-driven fixpoint (`reachability.py`), exercised against a
fake `run_query` that answers `F11_REACHABILITY_ROUND` over a small
in-memory graph the test builds by hand — the "tiny synthetic cell" for
this one family, chosen small enough that the expected earliest-arrival
table can be read off by inspection and cross-checked by brute-force BFS
in the test itself (no Neo4j, no TGMS import).
"""
from neo4j_recompute.reachability import run_reachability


def make_fake_query(edges):
    """`edges`: list of (src, dst, vt_s, vt_e). Answers one
    `F11_REACHABILITY_ROUND` round exactly as the Cypher text would:
    group by destination, keep the minimum relaxed arrival."""

    def run_query(cypher, params):
        front = params["front"]
        t_a, t_b = params["t_a"], params["t_b"]
        best_this_round: dict[str, int] = {}
        for f in front:
            for src, dst, vt_s, vt_e in edges:
                if src != f["uid"]:
                    continue
                if not (vt_e > t_a and vt_s < t_b):
                    continue
                tau = max(f["arr"], vt_s)
                if tau < vt_e and tau < t_b and (
                        dst not in best_this_round or tau < best_this_round[dst]):
                    best_this_round[dst] = tau
        return [{"uid": u, "arr": a} for u, a in best_this_round.items()]

    return run_query


def brute_force_earliest_arrival(edges, src, t_a, t_b):
    """Reference: relax every edge repeatedly until no label improves —
    exactly `tgms.temporal.ops_paths.temporal_reachability`'s own
    vectorized fixpoint, just written as a plain loop here."""
    best = {src: t_a}
    changed = True
    while changed:
        changed = False
        for s, d, vt_s, vt_e in edges:
            if s not in best:
                continue
            tau = max(best[s], vt_s)
            if tau < vt_e and tau < t_b and (d not in best or tau < best[d]):
                best[d] = tau
                changed = True
    return {u: a for u, a in best.items() if u != src}


def test_linear_chain_three_hops():
    edges = [("a", "b", 0, 100), ("b", "c", 5, 100), ("c", "d", 10, 100)]
    got = run_reachability(make_fake_query(edges), "a", 0, 1000)
    want = brute_force_earliest_arrival(edges, "a", 0, 1000)
    assert {r["uid"]: r["earliest_arrival"] for r in got} == want


def test_rows_sorted_by_arrival_then_uid():
    edges = [("a", "x", 0, 100), ("a", "y", 0, 100), ("a", "z", 5, 100)]
    got = run_reachability(make_fake_query(edges), "a", 0, 1000)
    assert [r["uid"] for r in got] == ["x", "y", "z"]
    assert [r["earliest_arrival"] for r in got] == [0, 0, 5]


def test_src_excluded_even_with_a_cycle_back_to_it():
    edges = [("a", "b", 0, 100), ("b", "a", 1, 100)]
    got = run_reachability(make_fake_query(edges), "a", 0, 1000)
    assert "a" not in {r["uid"] for r in got}


def test_diamond_takes_the_earlier_arrival():
    # a->b (fast) ->d, and a->c (slow)->d: d's earliest arrival must be via b
    edges = [("a", "b", 0, 100), ("a", "c", 0, 100),
             ("b", "d", 1, 100), ("c", "d", 50, 100)]
    got = run_reachability(make_fake_query(edges), "a", 0, 1000)
    d_row = next(r for r in got if r["uid"] == "d")
    assert d_row["earliest_arrival"] == 1


def test_unreachable_node_is_absent():
    edges = [("a", "b", 0, 100)]
    got = run_reachability(make_fake_query(edges), "a", 0, 1000)
    assert {r["uid"] for r in got} == {"b"}


def test_window_prunes_edges_outside_t_b():
    edges = [("a", "b", 0, 100)]
    got = run_reachability(make_fake_query(edges), "a", 0, 50)
    # the edge is valid on [0,100) but the window ends at 50; the arrival
    # at b (max(0, vt_s)=0) is < t_b=50, so it IS still reachable here —
    # use a case where the earliest relaxation itself falls outside t_b
    edges2 = [("a", "b", 60, 100)]
    got2 = run_reachability(make_fake_query(edges2), "a", 0, 50)
    assert got2 == []
    assert got != []  # sanity: the first case does reach b
