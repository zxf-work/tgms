"""M3.2 — `PatternMatch`, including the bo33 flagship gate.

Two things are being established. First that the **staged expansion is the same
relation** as the pattern it compiles — §6's BI11 note says so, and a
brute-force enumeration over a small fixture proves it rather than trusting the
note. Second that bo33's nine-edge motif **admits and completes on the real
bitcoin-otc store**, with its count agreeing with an independently written
brute force over the raw edge list — the gold-in-miniature for M3.4's bo33 gold.

The bo33 test skips when `stores/bitcoinotc` is absent (the directory is
gitignored), so it is a local gate rather than a CI one. Its measured result is
recorded in the phase report.
"""

from __future__ import annotations

import itertools
from collections import defaultdict
from pathlib import Path

import pytest

from tgms.temporal.algebra import ensure_all_registered
from tgms.temporal.props import parse_props
from tgms.tgir.eval import evaluate_core
from tgms.tgir.expr import Col
from tgms.tgir.node import (
    Agg, Aggregate, EdgePat, EdgeScan, NodePat, Pattern, PatternMatch, Project,
    PropertyPredicate, Source,
)
from tgms.tgir.types import Sigma

from .conftest import fresh_adapter

BITCOINOTC = Path(__file__).resolve().parents[1] / "stores/bitcoinotc"


def write(adapter, ops, tt):
    adapter.begin()
    adapter.apply_ops(ops, tt)
    adapter.commit()


def node_op(uid, label="N", props=None, vt_s=0, vt_e=100):
    return {"op": "assert_node", "uid": uid, "label": label, "props": props or {},
            "vt_s": vt_s, "vt_e": vt_e, "source": "i", "provenance_ref": None}


def edge_op(src, dst, rel_type="R", props=None, vt_s=0, vt_e=100, disc=""):
    return {"op": "assert_edge", "src": src, "dst": dst, "rel_type": rel_type,
            "props": props or {}, "vt_s": vt_s, "vt_e": vt_e, "disc": disc,
            "source": "i", "provenance_ref": None}


@pytest.fixture()
def triangles():
    """A store with two directed triangles sharing an edge, plus a dangling
    node — small enough to enumerate by hand, structured enough that a wrong
    join order or a missing isomorphism check changes the answer."""
    ensure_all_registered()
    a = fresh_adapter()
    write(a, [node_op(u) for u in ("a", "b", "c", "d", "e")], 1)
    write(a, [edge_op("a", "b"), edge_op("b", "c"), edge_op("c", "a"),
              edge_op("b", "d"), edge_op("d", "a"), edge_op("a", "e")], 2)
    yield a
    a.close()


def cycle_pattern():
    return Pattern((NodePat("x"), NodePat("y"), NodePat("z")),
                   (EdgePat("e1", "x", "y", "R"), EdgePat("e2", "y", "z", "R"),
                    EdgePat("e3", "z", "x", "R")))


def brute_force_cycles(adapter):
    """Every directed 3-cycle, enumerated over the raw edge list — no shared
    code with the evaluator, which is the point."""
    cols = adapter.edges_columnar(columns=("src_id", "dst_id", "eid", "vt_s",
                                           "vid", "rel_type"))
    uids = adapter.uids_for(list(range(adapter.num_entities())))
    edges = [(uids[cols["src_id"][i]], uids[cols["dst_id"][i]], cols["eid"][i])
             for i in range(len(cols["src_id"])) if cols["rel_type"][i] == "R"]
    out = set()
    for (s1, d1, i1), (s2, d2, i2), (s3, d3, i3) in itertools.permutations(edges, 3):
        if d1 == s2 and d2 == s3 and d3 == s1 and len({i1, i2, i3}) == 3:
            out.add((s1, d1, d2))
    return out


def test_the_staged_join_is_the_same_relation_as_the_pattern(triangles):
    """§6's BI11 note — "the staged plan and one triangle match are the same
    relation" — checked rather than trusted."""
    got = evaluate_core(PatternMatch(cycle_pattern(), sigma_=Sigma.default()),
                        triangles)
    found = {(r["x.uid"], r["y.uid"], r["z.uid"]) for r in got.rows()}
    assert found == brute_force_cycles(triangles)
    assert found, "the fixture must actually contain cycles"


def test_edge_isomorphism_is_over_identities_not_versions(triangles):
    """§8.5 CLOSED. Two *versions* of one `eid` must never bind two edge
    variables: a version-based rule would let two carve fragments of one
    logical edge manufacture pattern instances no uncorrected store has.
    """
    before = evaluate_core(PatternMatch(cycle_pattern()), triangles).n
    # a correction carves `a → b` into fragments without changing the graph
    write(triangles, [{"op": "correct",
                       "ref": {"kind": "edge", "src": "a", "dst": "b",
                               "rel_type": "R", "disc": ""},
                       "props": {"w": 1}, "vt_s": 40, "vt_e": 60,
                       "source": "i", "provenance_ref": None}], 3)
    after = evaluate_core(PatternMatch(cycle_pattern()), triangles)
    per_match = {(r["x.uid"], r["y.uid"], r["z.uid"]) for r in after.rows()}
    assert per_match == brute_force_cycles(triangles)
    assert after.n >= before
    for row in after.rows():
        eids = {row[f"e{i}.eid"] for i in (1, 2, 3)}
        assert len(eids) == 3, "nine variables, nine distinct identities"


def test_node_variables_are_not_implicitly_distinct(triangles):
    """"node variables are **not** implicitly distinct" — where a workload
    wants distinctness it writes a `Filter` (bo37's case).

    A two-hop path whose ends may coincide is the clean demonstration: `x` and
    `z` are different *variables*, and `a → b → a` binds them to the same node
    while `e1` and `e2` stay distinct identities, which is all
    edge-isomorphism asks.
    """
    write(triangles, [edge_op("b", "a")], 4)      # makes a → b → a walkable
    pattern = Pattern((NodePat("x"), NodePat("y"), NodePat("z")),
                      (EdgePat("e1", "x", "y", "R"), EdgePat("e2", "y", "z", "R")))
    got = evaluate_core(PatternMatch(pattern), triangles)
    assert any(r["x.uid"] == r["z.uid"] for r in got.rows()), \
        "x and z may bind one node; only an explicit Filter would stop them"
    assert all(r["e1.eid"] != r["e2.eid"] for r in got.rows())


def test_sources_restricts_a_variable_domain_without_changing_semantics(triangles):
    """§2.9: "the pushed and un-pushed forms must be semantically identical;
    only their cost estimates differ"."""
    scan = EdgeScan("s", rel_types=("R",))
    unpushed = evaluate_core(PatternMatch(cycle_pattern()), triangles)
    pushed = evaluate_core(
        PatternMatch(cycle_pattern(),
                     tuple(Source(f"e{i}", scan) for i in (1, 2, 3))), triangles)
    assert {(r["x.uid"], r["y.uid"], r["z.uid"]) for r in pushed.rows()} == \
        {(r["x.uid"], r["y.uid"], r["z.uid"]) for r in unpushed.rows()}


def test_edge_times_are_exposed_to_filter(triangles):
    """R1's substance: bound edges expose `vt_s`/`vt_e`, which is what makes
    BI11's creation-date window an ordinary row-local predicate."""
    got = evaluate_core(PatternMatch(cycle_pattern()), triangles)
    assert "e1.vt_s" in got.schema.names
    assert all(isinstance(r["e1.vt_s"], int) for r in got.rows())


def test_pattern_canonical_order(triangles):
    """§2.9: lexicographic over bound edge `(vt_s, vid)` in declaration order,
    then bound node `uid` in declaration order."""
    got = evaluate_core(PatternMatch(cycle_pattern()), triangles)
    keys = [tuple((int(r[f"e{i}.vt_s"]), str(r[f"e{i}.vid"])) for i in (1, 2, 3))
            + tuple(str(r[f"{v}.uid"]) for v in ("x", "y", "z"))
            for r in got.rows()]
    assert keys == sorted(keys)


def test_a_pattern_binds_node_columns_under_sigma(triangles):
    got = evaluate_core(PatternMatch(cycle_pattern()), triangles)
    assert "x.label" in got.schema.names and "x.tt_s" not in got.schema.names
    assert all(r["x.label"] == "N" for r in got.rows())


def test_a_node_pattern_label_filters_the_match(triangles):
    write(triangles, [node_op("b", label="OTHER")], 5)
    pattern = Pattern((NodePat("x"), NodePat("y", "OTHER"), NodePat("z")),
                      cycle_pattern().edge_pats)
    got = evaluate_core(PatternMatch(pattern), triangles)
    assert all(r["y.uid"] == "b" for r in got.rows())
    assert got.n >= 1


# ---------------------------------------------------------------------------
# the flagship: bo33 on the real store
# ---------------------------------------------------------------------------

BO33_EDGES = [("e12", "r1", "r2"), ("e21", "r2", "r1"),
              ("e13", "r1", "r3"), ("e31", "r3", "r1"),
              ("e23", "r2", "r3"), ("e32", "r3", "r2"),
              ("f1", "r1", "x"), ("f2", "r2", "x"), ("f3", "r3", "x")]


def bo33_plan():
    """§7.2's worked plan, verbatim: a mutually-positive rater triangle and the
    accounts all three endorse. Four node variables, nine edge variables — the
    shape that would break if `PatternMatch` were a fixed shape catalogue,
    which is the whole point of R1."""
    s2 = PropertyPredicate(EdgeScan("e", rel_types=("TRUST",)), "e", "rating",
                           ">", 0)
    pattern = Pattern(
        tuple(NodePat(v) for v in ("r1", "r2", "r3", "x")),
        tuple(EdgePat(var, src, dst, "TRUST") for var, src, dst in BO33_EDGES))
    s3 = PatternMatch(pattern, tuple(Source(var, s2) for var, _, _ in BO33_EDGES))
    return Aggregate(Project(s3, (("x", Col("x.uid")),)), (),
                     (Agg("count_distinct", "n", Col("x")),))


def bo33_brute_force(adapter) -> int:
    """The independent checker: positive-rating adjacency over the raw edge
    list, mutual triangles by set intersection, then the accounts all three
    endorse — with the same nine-distinct-identities rule the pattern applies.

    Shares no code with the evaluator, which is what makes it gold.
    """
    cols = adapter.edges_columnar(columns=("src_id", "dst_id", "rel_type", "props"))
    uids = adapter.uids_for(list(range(adapter.num_entities())))
    positive = set()
    for i in range(len(cols["src_id"])):
        if cols["rel_type"][i] != "TRUST":
            continue
        rating = parse_props(cols["props"][i]).get("rating")
        if isinstance(rating, (int, float)) and not isinstance(rating, bool) \
                and rating > 0:
            positive.add((uids[cols["src_id"][i]], uids[cols["dst_id"][i]]))

    out = defaultdict(set)
    mutual = defaultdict(set)
    for src, dst in positive:
        out[src].add(dst)
    for src, dst in positive:
        if (dst, src) in positive:
            mutual[src].add(dst)

    targets = set()
    for u in sorted(mutual):
        for v in mutual[u]:
            if v <= u:
                continue
            for w in mutual[u] & mutual[v]:
                if w <= v:
                    continue
                for x in out[u] & out[v] & out[w]:
                    pairs = {(u, v), (v, u), (u, w), (w, u), (v, w), (w, v),
                             (u, x), (v, x), (w, x)}
                    if len(pairs) == 9:      # nine variables, nine identities
                        targets.add(x)
    return len(targets)


@pytest.mark.skipif(not BITCOINOTC.exists(),
                    reason="stores/bitcoinotc is gitignored; local gate only")
def test_bo33_admits_completes_and_agrees_with_brute_force():
    """M3.2's flagship gate. ~35k edge versions, nine edge variables, one
    `rating > 0` pushdown into every variable's domain — which is "what keeps
    Bitcoin-OTC admissible"."""
    import tgms

    ensure_all_registered()
    store = tgms.open(BITCOINOTC, read_only=True)
    try:
        got = evaluate_core(bo33_plan(), store.adapter, admit_plan=True)
        assert got.n == 1
        assert got.rows()[0]["n"] == bo33_brute_force(store.adapter)
    finally:
        store.close()
