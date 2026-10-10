"""[tests] The top-k claim form: verifier rule, SQL adapter capture, cells."""

from __future__ import annotations

import sqlite3

import pytest

from tgms.evidence.adapter_sql import (
    TopKBlocked,
    boundary_is_strict,
    boundary_probe_sql,
    build_sql_ecqr,
    build_sql_topk_ecqr,
    parse_topk,
    ranked_page_in_snapshot,
    snapshot_context,
    sqlite_unique_keys,
)
from tgms.evidence.claims import ExactCount, TopK, normalize_order
from tgms.evidence.ecqr import ECQR, Basis, Ranking, Scope, basis_identity
from tgms.evidence.faultbench_topk import (
    CHECKERS,
    cases,
    judge,
    run_cells,
    truth,
)
from tgms.evidence.verify import Verdict, verify

ORDER = [["score", "desc"], ["uid", "asc"]]
TOP = [["a", 9], ["b", 7], ["c", 5]]


def _e(rows, *, limit=3, order=ORDER, total=True, delivery=False,
       execution=True, ranking=True, pinned=True, as_of=10):
    return ECQR(result_id="x",
                basis=Basis(store="s", as_of_tt=as_of, pinned=pinned),
                scope=Scope(domain={"sql": "q"},
                            execution_complete=execution,
                            delivery_complete=delivery,
                            rows_returned=len(rows)),
                ranking=Ranking(candidate_domain={"sql": "q"}, limit=limit,
                                order=order, order_total=total)
                if ranking else None)


def _c(rows=TOP, k=3, key=("score", "uid"), d=("desc", "asc"), **kw):
    return TopK(rows=[list(r) for r in rows], key=list(key), k=k,
                dir=list(d), **kw)


def _v(claim, e, rows):
    return verify(claim, e, {"rows": rows}).verdict


# ------------------------------------------------------------------ rule

def test_full_page_under_total_order_is_supported():
    assert _v(_c(), _e(TOP), TOP) is Verdict.SUPPORTED


def test_tuple_rows_match_list_rows():
    c = TopK(rows=[tuple(r) for r in TOP], key=["score", "uid"],
             dir=["desc", "asc"], k=3)
    assert _v(c, _e(TOP), TOP) is Verdict.SUPPORTED


def test_no_ranking_or_no_order_or_not_total():
    assert _v(_c(), _e(TOP, ranking=False), TOP) is \
        Verdict.UNSUPPORTED_ORDER_NOT_TOTAL
    assert _v(_c(), _e(TOP, order=[], total=False), TOP) is \
        Verdict.UNSUPPORTED_ORDER_NOT_TOTAL
    assert _v(_c(), _e(TOP, total=False), TOP) is \
        Verdict.UNSUPPORTED_ORDER_NOT_TOTAL


def test_claim_must_name_the_whole_recorded_order():
    coarse = _c(key=("score",), d=("desc",))
    assert _v(coarse, _e(TOP), TOP) is Verdict.UNSUPPORTED_ORDER_MISMATCH
    flipped = _c(d=("asc", "asc"))
    assert _v(flipped, _e(TOP), TOP) is Verdict.UNSUPPORTED_ORDER_MISMATCH
    bad = _c(d=("down", "asc"))
    assert _v(bad, _e(TOP), TOP) is Verdict.UNSUPPORTED_ORDER_MISMATCH


def test_execution_must_be_certified():
    assert _v(_c(), _e(TOP, execution=False), TOP) is \
        Verdict.UNSUPPORTED_EXECUTION_NOT_CERTIFIED


def test_k_must_equal_the_recorded_limit():
    assert _v(_c(k=5), _e(TOP), TOP) is Verdict.UNSUPPORTED_K_MISMATCH
    assert _v(_c(rows=TOP[:2], k=2), _e(TOP), TOP) is \
        Verdict.UNSUPPORTED_K_MISMATCH
    assert _v(_c(k=0), _e(TOP, limit=0), TOP) is \
        Verdict.UNSUPPORTED_K_MISMATCH
    four = TOP + [["d", 1]]
    assert _v(_c(rows=four), _e(four), four) is \
        Verdict.UNSUPPORTED_K_MISMATCH


def test_rows_must_be_the_delivered_sequence():
    swapped = [TOP[1], TOP[0], TOP[2]]
    assert _v(_c(rows=swapped), _e(TOP), TOP) is \
        Verdict.UNSUPPORTED_VALUE_MISMATCH


def test_short_page_needs_certified_delivery():
    short = TOP[:2]
    assert _v(_c(rows=short, k=5), _e(short, limit=5, delivery=True),
              short) is Verdict.SUPPORTED
    assert _v(_c(rows=short, k=5), _e(short, limit=5, delivery=False),
              short) is Verdict.UNSUPPORTED_COMPLETENESS_NOT_CERTIFIED


def test_basis_qualifier_applies():
    assert _v(_c(basis_tt=10), _e(TOP), TOP) is Verdict.SUPPORTED
    assert _v(_c(basis_tt=11), _e(TOP), TOP) is \
        Verdict.UNSUPPORTED_BASIS_MISMATCH


def test_normalize_order():
    assert normalize_order("x") == [["x", "asc"]]
    assert normalize_order(["x", "y"], "DESC") == [["x", "desc"],
                                                   ["y", "desc"]]
    with pytest.raises(ValueError):
        normalize_order(["x", "y"], ["asc"])


# ------------------------------------------------------------ descriptor

def test_unranked_descriptor_serializes_as_before():
    e = _e(TOP, ranking=False)
    d = e.to_json()
    assert "ranking" not in d
    assert ECQR.from_json(d) == e


def test_ranked_descriptor_round_trips():
    e = _e(TOP)
    d = e.to_json()
    assert d["ranking"]["order"] == ORDER
    assert ECQR.from_json(d) == e


# ---------------------------------------------------------------- adapter

@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    c.executescript("""
        CREATE TABLE p (id INTEGER PRIMARY KEY, name TEXT, score INT);
        CREATE TABLE u (code TEXT PRIMARY KEY, v INT);
        CREATE TABLE w (code TEXT NOT NULL PRIMARY KEY, v INT);
        CREATE TABLE x (a INT, b INT, PRIMARY KEY (a, b)) WITHOUT ROWID;
        INSERT INTO p VALUES (1,'a',9),(2,'b',7),(3,'c',7),(4,'d',1);
    """)
    yield c
    c.close()


def test_unique_keys(con):
    uk = sqlite_unique_keys(con)
    assert frozenset(["id"]) in uk["p"] and frozenset(["rowid"]) in uk["p"]
    assert uk["u"] == [frozenset(["rowid"]), frozenset(["_rowid_"]),
                       frozenset(["oid"])]          # nullable TEXT key
    assert frozenset(["code"]) in uk["w"]
    assert uk["x"] == [frozenset(["a", "b"])]       # no rowid


@pytest.mark.parametrize("sql,is_topk,reason,total", [
    ("SELECT name FROM p ORDER BY score DESC, id LIMIT 2", True,
     "unique_key_in_order", True),
    ("SELECT name FROM p ORDER BY score DESC LIMIT 2", True,
     "no_unique_key_in_order", False),
    ("SELECT name FROM p LIMIT 2", True, "no_order_by", False),
    ("SELECT name AS id FROM p ORDER BY id LIMIT 2", True,
     "no_unique_key_in_order", False),
    ("SELECT id AS k FROM p AS q ORDER BY q.score, k DESC LIMIT 2", True,
     "unique_key_in_order", True),
    ("SELECT name, id FROM p ORDER BY 2 LIMIT 2", True,
     "unique_key_in_order", True),
    ("SELECT name FROM p ORDER BY rowid LIMIT 2", True,
     "unique_key_in_order", True),
    ("SELECT v FROM u ORDER BY code LIMIT 2", True,
     "no_unique_key_in_order", False),
    ("SELECT v FROM w ORDER BY code LIMIT 2", True,
     "unique_key_in_order", True),
    ("SELECT a FROM x ORDER BY b, a LIMIT 2", True,
     "unique_key_in_order", True),
    ("SELECT p.name FROM p JOIN u ON 1 ORDER BY p.id LIMIT 2", True,
     "not_single_table", False),
    ("SELECT name, COUNT(*) c FROM p GROUP BY name ORDER BY c LIMIT 2",
     True, "grouped", False),
    ("SELECT DISTINCT name FROM p ORDER BY id LIMIT 2", True,
     "distinct_key_not_projected", False),
    ("SELECT name FROM (SELECT * FROM p) ORDER BY id LIMIT 2", True,
     "derived_or_qualified_source", False),
    ("SELECT name FROM p UNION SELECT code FROM u ORDER BY 1 LIMIT 2",
     True, "compound_query", False),
    ("SELECT name FROM p ORDER BY id LIMIT 2 OFFSET 1", False,
     "outer_offset", False),
    ("SELECT name FROM p ORDER BY id LIMIT 1, 2", False,
     "outer_offset", False),
    ("SELECT name FROM p WHERE id IN (SELECT id FROM p LIMIT 1)", False,
     "no_outer_limit", False),
    ("SELECT * FROM (", False, "parse_error", False),
])
def test_parse_topk(con, sql, is_topk, reason, total):
    s = parse_topk(sql, sqlite_unique_keys(con))
    assert (s.is_topk, s.reason, s.order_total) == (is_topk, reason, total)


def test_candidate_domain_strips_only_the_outer_limit(con):
    s = parse_topk("SELECT name FROM p WHERE id IN (SELECT id FROM p "
                   "ORDER BY score LIMIT 3) ORDER BY id DESC LIMIT 2",
                   sqlite_unique_keys(con))
    assert s.limit == 2 and s.order == [["id", "desc"]]
    assert "LIMIT 3" in s.candidate_sql and "LIMIT 2" not in s.candidate_sql
    assert con.execute(s.candidate_sql).fetchall()


def _run(con, sql):
    shape = parse_topk(sql, sqlite_unique_keys(con))
    rows = [list(r) for r in con.execute(sql).fetchall()]
    n = con.execute(f"SELECT COUNT(*) FROM ({shape.candidate_sql})"
                    ).fetchone()[0]
    e = build_sql_topk_ecqr(rows=rows, sql=sql, shape=shape,
                            store_id="t", candidate_count=n)
    claim = TopK(rows=rows, key=[p[0] for p in shape.order],
                 dir=[p[1] for p in shape.order], k=shape.limit)
    return e, claim, rows


def test_end_to_end_certified(con):
    e, claim, rows = _run(con, "SELECT name FROM p ORDER BY score DESC, "
                               "id LIMIT 2")
    assert rows == [["a"], ["b"]]
    assert not e.scope.delivery_complete and e.scope.exact_cardinality == 4
    assert e.scope.domain == e.ranking.candidate_domain
    assert verify(claim, e, {"rows": rows}).verdict is Verdict.SUPPORTED


def test_end_to_end_tie_is_refused(con):
    e, claim, rows = _run(con, "SELECT name FROM p ORDER BY score DESC "
                               "LIMIT 2")
    assert verify(claim, e, {"rows": rows}).verdict is \
        Verdict.UNSUPPORTED_ORDER_NOT_TOTAL


def test_end_to_end_exhausted_page(con):
    sql = "SELECT name FROM p ORDER BY id LIMIT 10"
    shape = parse_topk(sql, sqlite_unique_keys(con))
    rows = [list(r) for r in con.execute(sql).fetchall()]
    e = build_sql_topk_ecqr(rows=rows, sql=sql, shape=shape, store_id="t")
    assert e.scope.delivery_complete and e.scope.exact_cardinality == 4
    claim = TopK(rows=rows, key="id", dir="asc", k=10)
    assert verify(claim, e, {"rows": rows}).verdict is Verdict.SUPPORTED
    # the certificate is over Q', so it backs an ExactCount of Q' too
    assert verify(ExactCount(n=4), e, {"rows": rows}).verdict is \
        Verdict.SUPPORTED


def test_ranking_over_an_incomplete_input_is_blocked(con):
    sql = "SELECT name FROM p ORDER BY id LIMIT 2"
    shape = parse_topk(sql, sqlite_unique_keys(con))
    page = _e(TOP, delivery=False)
    with pytest.raises(TopKBlocked):
        build_sql_topk_ecqr(rows=[["a"], ["b"]], sql=sql, shape=shape,
                            store_id="t", input_ecqrs=[page])


def test_non_topk_statement_is_refused(con):
    sql = "SELECT name FROM p ORDER BY id LIMIT 2 OFFSET 1"
    shape = parse_topk(sql, sqlite_unique_keys(con))
    with pytest.raises(ValueError):
        build_sql_topk_ecqr(rows=[["b"], ["c"]], sql=sql, shape=shape,
                            store_id="t")


def test_default_sql_descriptor_is_unchanged(con):
    e = build_sql_ecqr(rows=[["a"]], sql="SELECT name FROM p LIMIT 1",
                       store_id="t", total_count=1, limited=True)
    assert e.ranking is None and "ranking" not in e.to_json()
    assert verify(TopK(rows=[["a"]], key="id", k=1), e,
                  {"rows": [["a"]]}).verdict is \
        Verdict.UNSUPPORTED_ORDER_NOT_TOTAL


# ------------------------------------------------------------------ cells

def test_cells_meet_their_expected_verdicts():
    cells = cases()
    assert len(cells) >= 8
    for c in cells:
        for ch in CHECKERS:
            assert judge(c, ch) == c.expected[ch], (c.fault, ch)


def test_cell_expectations_follow_the_oracle():
    for c in cases():
        assert truth(c) == (c.expectation == "must_certify"), c.fault


def test_ecqr_has_no_false_outcomes_and_baselines_do():
    r = run_cells()["checkers"]
    assert r["ecqr"] == {"false_certifications": [], "false_rejections": []}
    assert len(r["b1_value_only"]["false_certifications"]) == 11
    assert r["b2_taint_all"]["false_certifications"] == ["wrong_snapshot"]
    assert r["b2_taint_all"]["false_rejections"] == [
        "clean", "set_boundary_strict_inner_tie"]


def test_blocking_is_what_stops_the_over_page_cell():
    # the descriptor a non-blocking executor would emit is trace-relative
    # truth (it IS the top-2 of the page it ranked), so the verifier
    # alone accepts it; only rule (a) keeps the full-input claim out
    c = next(c for c in cases() if c.fault == "over_truncated_input")
    assert verify(c.claim, c.unblocked, c.result).verdict is \
        Verdict.SUPPORTED


# ------------------------------------------------------ boundary-strict route

COARSE = [["score", "desc"]]
TIE_IN = [["a", 9], ["b", 9]]          # tie inside the top 2, 9 > 7 below


def _eb(rows, *, boundary=True, bb_as_of=10, limit=2, delivery=False):
    e = _e(rows, limit=limit, order=COARSE, total=False, delivery=delivery)
    e.ranking.boundary_strict = boundary
    e.ranking.boundary_basis = basis_identity(Basis(
        store="s", as_of_tt=bb_as_of, pinned=True)) if boundary else None
    return e


def _cs(rows, k=2, as_set=True):
    return TopK(rows=[list(r) for r in rows], key="score", dir="desc", k=k,
                as_set=as_set)


def test_set_claim_certifies_on_a_strict_boundary_and_names_the_route():
    j = verify(_cs(list(reversed(TIE_IN))), _eb(TIE_IN), {"rows": TIE_IN})
    assert j.verdict is Verdict.SUPPORTED and j.route == "boundary_strict"


def test_sequence_claim_is_refused_on_the_set_route():
    assert _v(_cs(TIE_IN, as_set=False), _eb(TIE_IN), TIE_IN) is \
        Verdict.UNSUPPORTED_ORDER_NOT_TOTAL


def test_no_strict_boundary_refuses_the_set_claim():
    assert _v(_cs(TIE_IN), _eb(TIE_IN, boundary=False), TIE_IN) is \
        Verdict.UNSUPPORTED_ORDER_NOT_TOTAL


def test_boundary_checked_on_another_basis_is_a_basis_mismatch():
    assert _v(_cs(TIE_IN), _eb(TIE_IN, bb_as_of=11), TIE_IN) is \
        Verdict.UNSUPPORTED_BASIS_MISMATCH


def test_set_route_keeps_the_other_obligations():
    e = _eb(TIE_IN)
    e.scope.execution_complete = False
    assert _v(_cs(TIE_IN), e, TIE_IN) is \
        Verdict.UNSUPPORTED_EXECUTION_NOT_CERTIFIED
    assert _v(_cs(TIE_IN, k=3), _eb(TIE_IN), TIE_IN) is \
        Verdict.UNSUPPORTED_K_MISMATCH
    assert _v(_cs([["a", 9], ["z", 9]]), _eb(TIE_IN), TIE_IN) is \
        Verdict.UNSUPPORTED_VALUE_MISMATCH
    short = TIE_IN[:1]
    assert _v(_cs(short, k=2), _eb(short), short) is \
        Verdict.UNSUPPORTED_COMPLETENESS_NOT_CERTIFIED
    assert _v(_cs(short, k=2), _eb(short, delivery=True), short) is \
        Verdict.SUPPORTED


def test_total_order_route_serves_set_claims_too():
    c = TopK(rows=list(reversed(TOP)), key=["score", "uid"],
             dir=["desc", "asc"], k=3, as_set=True)
    j = verify(c, _e(TOP), {"rows": TOP})
    assert j.verdict is Verdict.SUPPORTED and j.route == "total_order"


def test_boundary_decoding():
    assert boundary_is_strict(2, None, 2)      # fewer than k+1 rows
    assert boundary_is_strict(3, 3, 2)
    assert not boundary_is_strict(3, 2, 2)     # rank k+1 tied with rank k
    assert not boundary_is_strict(None, None, 2)


def test_boundary_probe_unavailable_shapes():
    assert boundary_probe_sql("SELECT DISTINCT a FROM t ORDER BY a", 1) is None
    assert boundary_probe_sql("SELECT a FROM t", 1) is None
    assert boundary_probe_sql("SELECT a FROM t UNION SELECT b FROM u "
                              "ORDER BY 1", 1) is None


@pytest.mark.parametrize("sql,strict", [
    ("SELECT name FROM p ORDER BY score DESC LIMIT 1", True),     # 9 > 7
    ("SELECT name FROM p ORDER BY score DESC LIMIT 2", False),    # 7 = 7
    ("SELECT name FROM p ORDER BY score DESC LIMIT 3", True),     # 7 > 1
    ("SELECT name FROM p ORDER BY score DESC LIMIT 9", True),     # n <= k
    ("SELECT name, score AS s FROM p ORDER BY s, 1 LIMIT 2", True),
])
def test_ranked_page_in_snapshot(con, sql, strict):
    shape = parse_topk(sql, sqlite_unique_keys(con))
    r = ranked_page_in_snapshot(con, sql, shape)
    assert r.boundary_status == "checked" and r.boundary_strict is strict
    assert r.candidate_count == 4 and r.token["consistent"]
    assert set(r.seconds) == {"page", "boundary", "count_and_commit"}
    rows = [list(x) for x in r.rows]
    e = build_sql_topk_ecqr(rows=rows, sql=sql, shape=shape, store_id="t",
                            candidate_count=r.candidate_count,
                            execution_context=snapshot_context(r.token),
                            boundary_strict=r.boundary_strict)
    assert e.ranking.boundary_basis == (basis_identity(e.basis)
                                        if strict else None)
    claim = TopK(rows=rows, key=[p[0] for p in shape.order],
                 dir=[p[1] for p in shape.order], k=shape.limit,
                 as_set=True)
    j = verify(claim, e, {"rows": rows})
    assert (j.verdict is Verdict.SUPPORTED) is (strict or shape.order_total)


def test_nulls_follow_the_engine_order(con):
    con.execute("INSERT INTO p VALUES (5, 'e', NULL)")
    con.commit()
    sql = "SELECT name FROM p ORDER BY score LIMIT 1"   # NULL sorts first
    shape = parse_topk(sql, sqlite_unique_keys(con))
    r = ranked_page_in_snapshot(con, sql, shape)
    assert r.rows == [("e",)] and r.boundary_strict
