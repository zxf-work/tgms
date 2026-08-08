"""[tests] The M3 portability suite: one store, two execution surfaces,
one verifier, compatible judgments.

The same logical content is queried through TGMS operators and through
direct SQL over the same DuckDB file; both surfaces produce ECQRs through
their own adapters; the SAME generic verifier judges the same claims over
both. The exit gate this pins: judgments agree per claim class, and the
verifier core contains no backend branches (the purity test in
test_evidence.py covers the import side; this file covers behavior).
"""

from __future__ import annotations

import json

import pytest

import tgms
from tgms.core.model import OPEN_END
from tgms.evidence import (
    CompleteSet,
    ExactCount,
    Membership,
    Nonexistence,
    Verdict,
    verify,
)
from tgms.evidence.adapter_sql import build_sql_ecqr
from tgms.evidence.adapter_tgms import build_ecqr

CURRENT = f"tt_e = {OPEN_END}"


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    from tgms.data.synth import generate
    from tgms.temporal.algebra import call_operator
    from tgms.tools.server import ensure_all_registered
    ensure_all_registered()
    tmp = tmp_path_factory.mktemp("port")
    generate(tmp / "synth", n_nodes=40, n_events=600, seed=7)
    store = tgms.open(tmp / "store", backend="duckdb")
    with open(tmp / "synth" / "events.jsonl") as f:
        store.ingest_events(json.loads(line) for line in f if line.strip())
    stats = store.stats()
    return {"store": store, "conn": store.adapter.conn,
            "call": call_operator,
            "t_a": stats["vt_min"], "t_b": stats["vt_max"] + 2}


def _tgms_distinct_src(env, limit):
    env_ = env["call"](env["store"].adapter, "aggregate_events", {
        "window": {"t_a": env["t_a"], "t_b": env["t_b"]},
        "group_by": [{"dim": "endpoint", "role": "src"}],
        "aggregates": [{"agg": "count"}], "limit": limit})
    return env_, build_ecqr(env_, store_id="port-store")


def _sql_distinct_src(env, limit):
    where = (f"vt_s >= {env['t_a']} AND vt_s < {env['t_b']} AND {CURRENT}")
    total = env["conn"].execute(
        f"SELECT COUNT(DISTINCT src) FROM edge_versions WHERE {where}"
    ).fetchone()[0]
    sql = (f"SELECT DISTINCT src FROM edge_versions WHERE {where} "
           f"ORDER BY src LIMIT {limit}")
    rows = [r[0] for r in env["conn"].execute(sql).fetchall()]
    return rows, build_sql_ecqr(rows=rows, sql=sql, store_id="port-store",
                                total_count=int(total),
                                limited=len(rows) >= limit)


def test_exact_count_certified_across_truncation_on_both_backends(env):
    t_env, t_ecqr = _tgms_distinct_src(env, limit=5)
    s_rows, s_ecqr = _sql_distinct_src(env, limit=5)
    n = t_env["rows_total"]
    assert n > 5, "fixture must overflow the page"
    assert s_ecqr.scope.exact_cardinality == n  # backends agree on the fact
    for ecqr, result in ((t_ecqr, t_env), (s_ecqr, {"rows": s_rows})):
        assert ecqr.scope.delivery_complete is False
        assert verify(ExactCount(n=n), ecqr,
                      result).verdict == Verdict.SUPPORTED
        assert verify(ExactCount(n=n + 1), ecqr,
                      result).verdict == Verdict.UNSUPPORTED_VALUE_MISMATCH


def test_complete_set_refused_on_truncated_pages_on_both_backends(env):
    t_env, t_ecqr = _tgms_distinct_src(env, limit=5)
    s_rows, s_ecqr = _sql_distinct_src(env, limit=5)
    t_members = [r["src"] for r in t_env["rows"]]
    for ecqr, result, members, fld in (
            (t_ecqr, t_env, t_members, "src"),
            (s_ecqr, {"rows": s_rows}, s_rows, None)):
        j = verify(CompleteSet(members=members, field=fld), ecqr, result)
        assert j.verdict == Verdict.UNSUPPORTED_INCOMPLETE


def test_membership_witnessed_on_both_backends(env):
    t_env, t_ecqr = _tgms_distinct_src(env, limit=5)
    s_rows, s_ecqr = _sql_distinct_src(env, limit=5)
    uid = t_env["rows"][0]["src"]
    assert verify(Membership(value=uid, field="src"), t_ecqr,
                  t_env).verdict == Verdict.SUPPORTED
    # the SQL page is ordered differently; membership needs its own witness
    witness = s_rows[0]
    assert verify(Membership(value=witness), s_ecqr,
                  {"rows": s_rows}).verdict == Verdict.SUPPORTED


def test_nonexistence_agrees_on_both_backends(env):
    empty_a, empty_b = env["t_b"] + 10, env["t_b"] + 20
    t_env = env["call"](env["store"].adapter, "aggregate_events", {
        "window": {"t_a": empty_a, "t_b": empty_b},
        "group_by": [{"dim": "endpoint", "role": "src"}],
        "aggregates": [{"agg": "count"}], "limit": 100})
    t_ecqr = build_ecqr(t_env, store_id="port-store")
    where = f"vt_s >= {empty_a} AND vt_s < {empty_b} AND {CURRENT}"
    sql = f"SELECT src FROM edge_versions WHERE {where}"
    s_rows = [r[0] for r in env["conn"].execute(sql).fetchall()]
    s_ecqr = build_sql_ecqr(rows=s_rows, sql=sql, store_id="port-store",
                            limited=False)
    assert verify(Nonexistence(), t_ecqr,
                  t_env).verdict == Verdict.SUPPORTED
    assert verify(Nonexistence(), s_ecqr,
                  {"rows": s_rows}).verdict == Verdict.SUPPORTED


def test_historical_basis_judged_identically(env):
    t = env["store"].clock.last_tt
    t_env = env["call"](env["store"].adapter, "aggregate_events", {
        "window": {"t_a": env["t_a"], "t_b": env["t_b"]}, "as_of_tt": t,
        "group_by": [{"dim": "endpoint", "role": "src"}],
        "aggregates": [{"agg": "count"}], "limit": 10000})
    t_ecqr = build_ecqr(t_env, store_id="port-store")
    where = (f"vt_s >= {env['t_a']} AND vt_s < {env['t_b']} "
             f"AND tt_s <= {t} AND {t} < tt_e")
    total = env["conn"].execute(
        f"SELECT COUNT(DISTINCT src) FROM edge_versions WHERE {where}"
    ).fetchone()[0]
    sql = f"SELECT DISTINCT src FROM edge_versions WHERE {where}"
    s_rows = [r[0] for r in env["conn"].execute(sql).fetchall()]
    s_ecqr = build_sql_ecqr(rows=s_rows, sql=sql, store_id="port-store",
                            as_of_tt=t, total_count=int(total),
                            limited=False)
    n = t_env["rows_total"]
    assert s_ecqr.scope.exact_cardinality == n
    for ecqr, result in ((t_ecqr, t_env), (s_ecqr, {"rows": s_rows})):
        assert ecqr.basis.pinned
        assert verify(ExactCount(n=n, basis_tt=t), ecqr,
                      result).verdict == Verdict.SUPPORTED
        assert verify(ExactCount(n=n, basis_tt=t - 1), ecqr,
                      result).verdict == Verdict.UNSUPPORTED_BASIS_MISMATCH
