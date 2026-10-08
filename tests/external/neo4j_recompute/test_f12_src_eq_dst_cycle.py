"""Regression test for `neo4j-recompute-temporal-paths-src-eq-dst-cycles`
(`ops/failure_ledger.jsonl`, found by lane EXT-S on `storm-000911`,
`synth-iv-60k-c3-days-n1000-s0`: `src == dst == "n368"`, a 3-hop cycle
through n368 closed 366 times instead of returning no paths).

This is the live cross-check `test_queries.py`'s `TestF12BranchGeneration`
cannot do by itself: it actually runs the generated Cypher against a real
Neo4j and checks the *answer*, not just the query text. It needs a live
bolt endpoint, so it is skipped unless `NEO4J_RECOMPUTE_TEST_BOLT_URI` is
set (and skipped, not failed, if that endpoint refuses the connection) --
there is no Neo4j install on the laptop; this runs on xzgpu.

Two tiny fixtures, loaded into (and cleaned out of) whatever database the
URI points at, both shaped as a 2-hop cycle `a -> b -> a`:

  * `src == dst`: TGMS's own semantics (the walk starts with `src` already
    visited, so no hop may re-enter it -- `tgms/temporal/ops_paths.py`'s
    `dfs`, seeded `visited={sid}`, and `tgms/temporal/oracle.py`'s mirror,
    seeded `visited={args["src"]}`) say this cycle is not a path to
    itself: expected rows_total == 0.
  * `src != dst` (same edges, different endpoints asked for): the fix must
    not touch this case -- expected the ordinary 1-hop path, unchanged.
"""
import os

import pytest

neo4j = pytest.importorskip("neo4j")

from neo4j_recompute import queries  # noqa: E402
from neo4j_recompute.canon import OPEN_END  # noqa: E402

BOLT_URI = os.environ.get("NEO4J_RECOMPUTE_TEST_BOLT_URI")


@pytest.fixture(scope="module")
def session():
    if not BOLT_URI:
        pytest.skip("NEO4J_RECOMPUTE_TEST_BOLT_URI not set -- no live Neo4j to test against")
    driver = neo4j.GraphDatabase.driver(BOLT_URI, auth=None)
    try:
        driver.verify_connectivity()
    except Exception as e:  # pragma: no cover - environment-dependent
        driver.close()
        pytest.skip(f"cannot reach Neo4j at {BOLT_URI}: {e}")
    sess = driver.session(database="neo4j")
    try:
        sess.run("CREATE CONSTRAINT e_uid IF NOT EXISTS "
                 "FOR (x:E) REQUIRE x.uid IS UNIQUE").consume()
        yield sess
    finally:
        sess.close()
        driver.close()


def _load_two_hop_cycle(session, prefix: str):
    """`prefix+'a' -> prefix+'b' -> prefix+'a'`, both hops inside
    `[0, 100)`, non-decreasing `vt_s` so the second hop's time pruning
    passes regardless of arrival. Cleaned up by the caller."""
    session.run(
        "MERGE (a:E {uid:$a}) MERGE (b:E {uid:$b}) "
        "CREATE (a)-[:EV {eid:$e1, vid:1, rel_type:'R', disc:0, "
        "vt_s:0, vt_e:100, tt_s:0, tt_e:$O, props:'{}', source:'t', "
        "provenance_ref:'t'}]->(b) "
        "CREATE (b)-[:EV {eid:$e2, vid:2, rel_type:'R', disc:0, "
        "vt_s:10, vt_e:100, tt_s:0, tt_e:$O, props:'{}', source:'t', "
        "provenance_ref:'t'}]->(a)",
        {"a": prefix + "a", "b": prefix + "b",
         "e1": prefix + "e1", "e2": prefix + "e2", "O": OPEN_END},
    ).consume()


def _delete_fixture(session, prefix: str):
    session.run("MATCH (x:E) WHERE x.uid IN [$a, $b] DETACH DELETE x",
                {"a": prefix + "a", "b": prefix + "b"}).consume()


def _run_f12(session, src, dst, max_hops=2, k=5):
    params, resolved_hops = queries.f12_params(
        {"src": src, "dst": dst, "window": {"t_a": 0, "t_b": 100},
         "k": k, "max_hops": max_hops})
    cypher = queries.f12_query(resolved_hops)
    result = session.run(cypher, params)
    return [dict(r) for r in result]


def test_src_eq_dst_cycle_is_not_a_path(session):
    """`a -> b -> a` queried with `src=dst=a`: the only candidate 2-hop
    "path" closes back onto the source on its last hop, which TGMS's own
    semantics forbid (see module docstring). Pre-fix, Neo4j's last hop was
    only pinned to `{uid:$dst}` with no `<>$src` guard, so it matched `a`
    itself and returned this cycle; post-fix it must not."""
    _load_two_hop_cycle(session, "fc_cyc_")
    try:
        rows = _run_f12(session, "fc_cyc_a", "fc_cyc_a")
        assert len(rows) == 1
        assert rows[0]["rows_total"] == 0
        assert rows[0]["rows"] == []
    finally:
        _delete_fixture(session, "fc_cyc_")


def test_src_ne_dst_path_is_unchanged(session):
    """Same two edges, but asked `src=a, dst=b`: an ordinary 1-hop path
    that never touches `src` again. The fix's `nL.uid<>$src` clause is
    vacuous here (`nL` is pinned to `dst`, and `dst != src`), so this must
    keep returning exactly the one hop it always did."""
    _load_two_hop_cycle(session, "fc_lin_")
    try:
        rows = _run_f12(session, "fc_lin_a", "fc_lin_b")
        assert len(rows) == 1
        assert rows[0]["rows_total"] == 1
        assert len(rows[0]["rows"]) == 1
        path = rows[0]["rows"][0]
        assert path["hops"] == 1
        assert path["edges"] == [{"src": "fc_lin_a", "dst": "fc_lin_b",
                                  "rel_type": "R", "eid": "fc_lin_e1", "t": 0}]
    finally:
        _delete_fixture(session, "fc_lin_")
