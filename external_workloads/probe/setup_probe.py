#!/usr/bin/env python
"""Established-interface truncation probe (D-142) — SETUP.

Builds the frozen probe universe BEFORE any inference, exactly per
external_workloads/probe/PROBE_FREEZE.md:

  SET family    every item of multiplicity_audit.jsonl (the 151
                set-encoded items) whose reference_rows > k = 10.
                Endpoint = the gold query, in gold execution order.
  COUNT family  the 76 auto EXACT_COUNT items relabelled
                SCALAR+CARDINALITY_VALUE by the D-132 errata, filtered
                by the two mechanical rules
                  (a) the counted domain is derivable by the D-131
                      projection-only rewrite AND validated against
                      the database (count over the derived domain
                      equals the gold value), and
                  (b) the gold count value exceeds k = 10.
                Endpoint = the validated counted domain.

`counted_domain_sql`, `count_shaped`, `_is_sum_shaped` and `integral`
below are ported VERBATIM from run_bird_agent.py at commit 5f07a11
(the D-131 rewrite), together with the validation the caller applied
there (count over the derived domain equals the gold count; for a
SUM-shaped aggregate the summand must additionally be 0/1-valued).

No sampling, no replacement, no post-hoc exclusion: every question
that passes the mechanical rules is in, and every question that fails
one is listed with its reason in the setup receipt
benchmarks/results-v1/eval-trunc-probe-setup.json.

    python external_workloads/probe/setup_probe.py \
        --frozen external_workloads/bird/bird_500_select_sqlite.jsonl \
        --audit external_workloads/bird/multiplicity_audit.jsonl \
        --errata external_workloads/bird/annotation_errata.jsonl \
        --db-root .../minidev/MINIDEV/dev_databases \
        --out external_workloads/probe/probe_manifest.jsonl \
        --receipt benchmarks/results-v1/eval-trunc-probe-setup.json \
        [--pg --pgdata external_workloads/probe/runs/pgdata]
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import sqlite3
import time
from pathlib import Path

import sqlglot
from sqlglot import expressions as exp

K = 10                     # page size, frozen
CEILING_S = 600            # the FREEZE.md gold-validation ceiling
BUDGET = 10                # tool calls per question, frozen
FREEZE_PATH = Path(__file__).resolve().parent / "PROBE_FREEZE.md"


def freeze_sha256() -> str:
    return hashlib.sha256(FREEZE_PATH.read_bytes()).hexdigest()


# ---------------------------------------------------------------- sqlite

def norm_cell(v):
    if isinstance(v, bytes):
        return {"b64": base64.b64encode(v).decode()}
    if isinstance(v, float) and not math.isfinite(v):
        return str(v)
    return v


def execute_sql(db: Path, sql: str, deadline_s: float = CEILING_S):
    """Read-only execution with a wall ceiling. Returns (rows, cols)."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    t0 = time.monotonic()
    con.set_progress_handler(
        lambda: 1 if time.monotonic() - t0 > deadline_s else 0, 10_000)
    try:
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description or []]
        rows = [[norm_cell(v) for v in r] for r in cur.fetchall()]
        return rows, cols
    finally:
        con.close()


def unique_columns(cols: list[str]) -> list[str]:
    """Record keys are column names; a repeated projection name would
    collide inside one JSON object, so later duplicates are suffixed."""
    seen: dict[str, int] = {}
    out = []
    for c in cols:
        c = c if c else "col"
        if c in seen:
            seen[c] += 1
            out.append(f"{c}__{seen[c]}")
        else:
            seen[c] = 1
            out.append(c)
    return out


def as_record(cols: list[str], row: list) -> dict:
    return {c: v for c, v in zip(cols, row)}


def canon(v) -> str:
    """Canonical comparison key for a record or an answered value."""
    return json.dumps(_canon_num(v), sort_keys=True,
                      separators=(",", ":"), ensure_ascii=False)


def _canon_num(v):
    if isinstance(v, dict):
        return {k: _canon_num(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_canon_num(x) for x in v]
    if isinstance(v, bool):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def page_sql(endpoint_sql: str, page: int, k: int = K) -> str:
    """A REST-style list endpoint page: at most k rows of the endpoint's
    own result list, zero-based, empty past the end."""
    return (f"SELECT * FROM ({endpoint_sql}) _p "
            f"LIMIT {int(k)} OFFSET {int(page) * int(k)}")


# -------------------------------------- D-131 rewrite (verbatim port)
# From run_bird_agent.py at 5f07a11; do not modify.

def count_shaped(sql: str) -> bool:
    """The outer projection IS a single Count/Sum aggregate (a bare
    count or a count-in-disguise like SUM(IIF(..))) over an ungrouped,
    unlimited outer query — the certificate shape. An arithmetic
    expression that merely CONTAINS a count (e.g. COUNT(*)/12) is not
    a cardinality and must never certify one."""
    try:
        tree = sqlglot.parse_one(sql, read="sqlite")
    except Exception:
        return False
    outer = tree
    while isinstance(outer, exp.Subquery):
        outer = outer.this
    if not isinstance(outer, exp.Select):
        return False
    if tree.args.get("limit") is not None or outer.args.get("group"):
        return False
    sels = outer.expressions
    if len(sels) != 1:
        return False
    node = sels[0]
    while isinstance(node, exp.Alias):
        node = node.this
    return isinstance(node, (exp.Count, exp.Sum))


def integral(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return None


def _is_sum_shaped(sql: str) -> bool:
    """Outer projection is SUM(...) — a count in disguise only if the
    summand is 0/1-valued, which the caller checks against the data."""
    try:
        tree = sqlglot.parse_one(sql, read="sqlite")
    except Exception:
        return False
    outer = tree
    while isinstance(outer, exp.Subquery):
        outer = outer.this
    if not isinstance(outer, exp.Select) or len(outer.expressions) != 1:
        return False
    node = outer.expressions[0]
    while isinstance(node, exp.Alias):
        node = node.this
    return isinstance(node, exp.Sum)


def counted_domain_sql(sql: str) -> str | None:
    """The query whose result set is exactly what `sql`'s aggregate
    counts, derived by replacing ONLY the projection.

    `ExactCount(n)` means |R*(Q)| = n for the descriptor's own domain
    Q. A count query's result has one row holding n, so a descriptor
    whose domain is `SELECT COUNT(*) FROM t WHERE p` must NOT carry
    exact_cardinality = n: its cardinality is 1. The cardinality
    claim belongs to the counted domain `SELECT * FROM t WHERE p`,
    which is what this derives and what the adapter's A2 obligation
    ("an unlimited count over the *same* predicate") already
    requires. Returns None when the shape is not derivable; the
    caller then falls back to a Scalar claim.
    """
    try:
        tree = sqlglot.parse_one(sql, read="sqlite")
    except Exception:
        return None
    outer = tree
    while isinstance(outer, exp.Subquery):
        outer = outer.this
    if not isinstance(outer, exp.Select) or len(outer.expressions) != 1:
        return None
    node = outer.expressions[0]
    while isinstance(node, exp.Alias):
        node = node.this

    inner = outer.copy()
    # a SELECT-level DISTINCT over a single ungrouped aggregate is a
    # no-op on the count but would wrongly dedupe the counted rows
    inner.set("distinct", None)

    if isinstance(node, exp.Count):
        arg = node.this
        distinct = False
        if isinstance(arg, exp.Distinct):
            distinct = True
            if len(arg.expressions) != 1:
                return None
            arg = arg.expressions[0]
        if isinstance(arg, exp.Star) or arg is None:
            if distinct:
                return None
            inner.set("expressions", [exp.Star()])
            return inner.sql(dialect="sqlite")
        inner.set("expressions", [exp.alias_(arg.copy(), "c")])
        kw = "DISTINCT " if distinct else ""
        # COUNT(x) ignores NULLs; the counted domain must too
        return (f"SELECT {kw}c FROM ({inner.sql(dialect='sqlite')}) _d "
                f"WHERE c IS NOT NULL")

    if isinstance(node, exp.Sum):
        # SUM(CASE WHEN p THEN 1 ELSE 0 END) and SUM(IIF(p,1,0)) are
        # counts in disguise; the caller validates that the summand is
        # 0/1-valued and that the domain size equals the sum.
        inner.set("expressions", [exp.alias_(node.this.copy(), "c")])
        return (f"SELECT c FROM ({inner.sql(dialect='sqlite')}) _d "
                f"WHERE c <> 0")

    return None


# ------------------------------------------------------------- manifest

def load_manifest(path: Path) -> list[dict]:
    items = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    return [it for it in items if not it["excluded"]]


def db_path(db_root: Path, db_id: str) -> Path:
    return db_root / db_id / f"{db_id}.sqlite"


def endpoint_probe(db: Path, endpoint_sql: str) -> tuple[int, list[str], int]:
    """(N, unique column names, page-0 JSON size) for an endpoint."""
    rows, _c = execute_sql(db, f"SELECT COUNT(*) FROM ({endpoint_sql}) _n")
    n = integral(rows[0][0])
    p0, cols = execute_sql(db, page_sql(endpoint_sql, 0))
    cols = unique_columns(cols)
    recs = [as_record(cols, r) for r in p0]
    return n, cols, len(json.dumps(recs, ensure_ascii=False))


def build_set_family(frozen: dict, audit: list[dict], db_root: Path):
    """Every set-encoded item with reference_rows > k. The endpoint is
    the gold query itself, in gold execution order."""
    kept, excluded = [], []
    for rec in audit:
        qid = rec["question_id"]
        if rec["reference_rows"] <= K:
            excluded.append({
                "question_id": qid, "family": "SET",
                "db_id": rec["db_id"], "reason": "reference_rows_le_k",
                "detail": f"reference_rows={rec['reference_rows']} <= k={K}"})
            continue
        q = frozen[qid]
        db = db_path(db_root, q["db_id"])
        sql = q["gold_sql"].strip().rstrip(";").strip()
        try:
            n, cols, p0b = endpoint_probe(db, sql)
        except Exception as e:
            excluded.append({
                "question_id": qid, "family": "SET", "db_id": q["db_id"],
                "reason": "endpoint_execution_error",
                "detail": f"{type(e).__name__}: {e}"[:200]})
            continue
        item = {
            "question_id": qid, "family": "SET", "db_id": q["db_id"],
            "question": q["question"], "endpoint_sql": sql, "N": n,
            "columns": cols, "page0_bytes": p0b,
            "audit_reference_rows": rec["reference_rows"],
            "audit_encoding": rec["encoding"], "excluded": False}
        if n != rec["reference_rows"]:
            # cannot happen for a re-executed pinned gold query; if it
            # ever did the universe would not be the frozen one
            item["N_disagrees_with_audit"] = True
        kept.append(item)
    return kept, excluded


def build_count_family(frozen: dict, errata_ids: list[int], db_root: Path):
    """The D-132 count items, filtered by freeze rules (a) then (b)."""
    kept, excluded = [], []
    for qid in errata_ids:
        q = frozen[qid]
        db = db_path(db_root, q["db_id"])
        sql = q["gold_sql"].strip().rstrip(";").strip()
        base = {"question_id": qid, "family": "COUNT", "db_id": q["db_id"]}

        # rule (a): derivable by the D-131 projection-only rewrite and
        # validated against the database
        if not count_shaped(sql):
            excluded.append({**base, "reason": "not_count_shaped",
                             "detail": "outer projection is not a single "
                                       "ungrouped, unlimited Count/Sum"})
            continue
        try:
            rows, _c = execute_sql(db, sql)
        except Exception as e:
            excluded.append({**base, "reason": "gold_execution_error",
                             "detail": f"{type(e).__name__}: {e}"[:200]})
            continue
        gold = (integral(rows[0][0])
                if len(rows) == 1 and len(rows[0]) == 1 else None)
        if gold is None or gold < 0:
            excluded.append({**base, "reason": "gold_value_not_a_count",
                             "detail": f"rows={len(rows)} value="
                                       f"{rows[0][0] if rows else None!r}"})
            continue
        cand = counted_domain_sql(sql)
        if cand is None:
            excluded.append({**base, "reason": "domain_not_derivable",
                             "detail": "projection-only rewrite does not "
                                       "apply to this aggregate shape"})
            continue
        try:
            vr, _v = execute_sql(db, f"SELECT COUNT(*) FROM ({cand}) _v")
            ok = integral(vr[0][0]) == gold
            detail = f"count(domain)={integral(vr[0][0])} gold={gold}"
            if ok and _is_sum_shaped(sql):
                br, _b = execute_sql(
                    db, f"SELECT COUNT(*) FROM ({cand}) _v WHERE c <> 1")
                ok = integral(br[0][0]) == 0
                if not ok:
                    detail = (f"summand not 0/1-valued: "
                              f"{integral(br[0][0])} rows with c<>1")
        except Exception as e:
            excluded.append({**base, "reason": "domain_execution_error",
                             "detail": f"{type(e).__name__}: {e}"[:200]})
            continue
        if not ok:
            excluded.append({**base, "reason": "domain_validation_failed",
                             "detail": detail})
            continue

        # rule (b): the gold count value exceeds k
        if gold <= K:
            excluded.append({**base, "reason": "gold_count_le_k",
                             "detail": f"gold count={gold} <= k={K}"})
            continue

        try:
            n, cols, p0b = endpoint_probe(db, cand)
        except Exception as e:
            excluded.append({**base, "reason": "endpoint_execution_error",
                             "detail": f"{type(e).__name__}: {e}"[:200]})
            continue
        assert n == gold, (qid, n, gold)
        kept.append({**base, "question": q["question"], "endpoint_sql": cand,
                     "N": n, "columns": cols, "page0_bytes": p0b,
                     "gold_count": gold, "gold_sql": sql,
                     "sum_shaped": _is_sum_shaped(sql), "excluded": False})
    return kept, excluded


def check_pagination(db: Path, item: dict) -> str | None:
    """The endpoint's pages must tile its own result list in order.
    Verified against a single full execution, at setup, for every
    eligible item; a failure would mean the paging mechanism is not
    faithful to the endpoint and is reported, never silently kept.

    Records are built POSITIONALLY from the manifest's frozen column
    names: a self-join can project the same name twice, and sqlite
    disambiguates a bare `SELECT *` and a wrapped one differently, so
    the raw description is not a stable record key."""
    cols = item["columns"]
    full, fc = execute_sql(db, item["endpoint_sql"])
    if len(fc) != len(cols):
        return f"full execution has {len(fc)} columns, manifest {len(cols)}"
    if len(full) != item["N"]:
        return f"full execution has {len(full)} rows, N={item['N']}"
    npages = (item["N"] + K - 1) // K
    for p in list(range(min(npages, 2))) + ([npages - 1] if npages > 2 else []):
        rows, rc = execute_sql(db, page_sql(item["endpoint_sql"], p))
        want = full[p * K:(p + 1) * K]
        if len(rc) != len(cols):
            return f"page {p} has {len(rc)} columns, manifest {len(cols)}"
        if [canon(as_record(cols, r)) for r in rows] != \
           [canon(as_record(cols, r)) for r in want]:
            return f"page {p} does not match the full execution slice"
    rows, _ = execute_sql(db, page_sql(item["endpoint_sql"], npages))
    if rows:
        return "the page past the end is not empty"
    return None


# ------------------------------------------------------------ postgres
# Engine-diversity leg: the relevant tables of the two smallest
# eligible databases are loaded verbatim into an embedded PostgreSQL
# (pgserver) and the same endpoints are served from it.

def pg_type(db: Path, table: str, col: str) -> str:
    """Verbatim load: the column's postgres type is decided by the
    values sqlite actually holds, not by the (dynamically typed)
    declaration."""
    rows, _ = execute_sql(
        db, f'SELECT DISTINCT typeof("{col}") FROM "{table}"')
    kinds = {r[0] for r in rows} - {"null"}
    if not kinds or kinds <= {"integer"}:
        return "BIGINT"
    if kinds <= {"integer", "real"}:
        return "DOUBLE PRECISION"
    if "blob" in kinds:
        return "BYTEA"
    return "TEXT"


def referenced_tables(sql: str) -> list[str]:
    tree = sqlglot.parse_one(sql, read="sqlite")
    names = []
    for t in tree.find_all(exp.Table):
        if t.name and t.name not in names:
            names.append(t.name)
    return names


def pg_load_tables(cur, db: Path, tables: list[str]) -> None:
    for t in tables:
        cols = [r[1] for r in execute_sql(
            db, f'PRAGMA table_info("{t}")')[0]]
        types = [pg_type(db, t, c) for c in cols]
        cur.execute(f'DROP TABLE IF EXISTS "{t}" CASCADE')
        cur.execute(f'CREATE TABLE "{t}" (' + ", ".join(
            f'"{c}" {ty}' for c, ty in zip(cols, types)) + ")")
        rows, _ = execute_sql(db, f'SELECT * FROM "{t}"')
        if not rows:
            continue
        vals = []
        for r in rows:
            out = []
            for v, ty in zip(r, types):
                if isinstance(v, dict) and "b64" in v:
                    out.append(base64.b64decode(v["b64"]))
                elif ty == "BIGINT" and isinstance(v, float):
                    out.append(int(v))
                else:
                    out.append(v)
            vals.append(tuple(out))
        ph = "(" + ",".join(["%s"] * len(cols)) + ")"
        args = b",".join(cur.mogrify(ph, v) for v in vals).decode()
        cur.execute(f'INSERT INTO "{t}" VALUES ' + args)


def pg_endpoint_table(qid: int) -> str:
    return f"probe_ep_{qid}"


def build_pg(items: list[dict], db_root: Path, pgdata: Path,
             n_dbs: int = 2):
    """Load the two smallest eligible databases and materialise their
    endpoints as ordered postgres tables. Returns (kept, excluded,
    dbs, uri)."""
    import pgserver
    import psycopg2

    sizes = {}
    for it in items:
        sizes.setdefault(it["db_id"],
                         db_path(db_root, it["db_id"]).stat().st_size)
    dbs = sorted(sizes, key=lambda d: sizes[d])[:n_dbs]

    pgdata.mkdir(parents=True, exist_ok=True)
    srv = pgserver.get_server(pgdata, cleanup_mode=None)
    uri = srv.get_uri()
    kept, excluded = [], []
    conn = psycopg2.connect(uri)
    conn.autocommit = True
    cur = conn.cursor()
    loaded: set[str] = set()
    for it in [i for i in items if i["db_id"] in dbs]:
        db = db_path(db_root, it["db_id"])
        base = {k: it[k] for k in ("question_id", "family", "db_id", "N")}
        try:
            tabs = [t for t in referenced_tables(it["endpoint_sql"])
                    if not t.startswith("_")]
            todo = [t for t in tabs if (it["db_id"], t) not in loaded]
            pg_load_tables(cur, db, todo)
            loaded.update((it["db_id"], t) for t in todo)
            pg_sql = sqlglot.transpile(it["endpoint_sql"], read="sqlite",
                                       write="postgres")[0]
        except Exception as e:
            excluded.append({**base, "reason": "pg_translation_error",
                             "detail": f"{type(e).__name__}: {e}"[:200]})
            continue
        tbl = pg_endpoint_table(it["question_id"])
        alias = ", ".join(f'"{c}"' for c in it["columns"])
        try:
            cur.execute(f'DROP TABLE IF EXISTS "{tbl}"')
            cur.execute(
                f'CREATE TABLE "{tbl}" AS SELECT ROW_NUMBER() OVER () '
                f'AS _ord, * FROM ({pg_sql}) AS _e({alias})')
            cur.execute(f'SELECT COUNT(*) FROM "{tbl}"')
            npg = cur.fetchone()[0]
        except Exception as e:
            excluded.append({**base, "reason": "pg_execution_error",
                             "detail": f"{type(e).__name__}: {e}"[:200]})
            continue
        if npg != it["N"]:
            excluded.append({**base, "reason": "pg_cardinality_mismatch",
                             "detail": f"postgres N={npg}, sqlite "
                                       f"N={it['N']}"})
            continue
        # the two engines must deliver the same result list (as a
        # multiset); order may differ and does not matter, the
        # endpoint's own order is frozen by _ord
        cur.execute(f'SELECT {alias} FROM "{tbl}" ORDER BY _ord')
        pg_rows = cur.fetchall()
        sq_rows, sc = execute_sql(db, it["endpoint_sql"])
        want = sorted(canon(as_record(it["columns"], list(r)))
                      for r in sq_rows)
        got = sorted(canon(as_record(it["columns"], [_pgval(v) for v in r]))
                     for r in pg_rows)
        if want != got:
            diff = next((i for i in range(min(len(want), len(got)))
                         if want[i] != got[i]), 0)
            excluded.append({**base, "reason": "engine_result_mismatch",
                             "detail": f"sqlite {want[diff][:80]} != "
                                       f"postgres {got[diff][:80]}"})
            cur.execute(f'DROP TABLE IF EXISTS "{tbl}"')
            continue
        kept.append({**{k: v for k, v in it.items()},
                     "engine": "pg", "endpoint_table": tbl,
                     "endpoint_sql_sqlite": it["endpoint_sql"],
                     "endpoint_sql_pg": pg_sql})
    cur.close()
    conn.close()
    return kept, excluded, dbs, uri


def _pgval(v):
    import datetime
    import decimal
    if isinstance(v, memoryview):
        return {"b64": base64.b64encode(bytes(v)).decode()}
    if isinstance(v, bytes):
        return {"b64": base64.b64encode(v).decode()}
    if isinstance(v, decimal.Decimal):
        f = float(v)
        return int(f) if f.is_integer() else f
    if isinstance(v, (datetime.date, datetime.datetime)):
        return v.isoformat()
    return v


# ------------------------------------------------------------------ main

def main() -> int:
    ap = argparse.ArgumentParser()
    here = Path(__file__).resolve().parent
    ap.add_argument("--frozen", type=Path)
    ap.add_argument("--audit", type=Path)
    ap.add_argument("--errata", type=Path)
    ap.add_argument("--db-root", type=Path)
    ap.add_argument("--out", type=Path,
                    default=here / "probe_manifest.jsonl")
    ap.add_argument("--out-pg", type=Path,
                    default=here / "probe_manifest_pg.jsonl")
    ap.add_argument("--receipt", type=Path)
    ap.add_argument("--pg", action="store_true",
                    help="also build the PostgreSQL engine-diversity leg")
    ap.add_argument("--pgdata", type=Path,
                    default=here / "runs" / "pgdata")
    ap.add_argument("--pg-stop", action="store_true",
                    help="stop the embedded postgres and exit")
    args = ap.parse_args()

    if args.pg_stop:
        import pgserver
        pgserver.get_server(args.pgdata, cleanup_mode="stop")
        print("embedded postgres stopped")
        return 0
    missing = [n for n in ("frozen", "audit", "errata", "db_root", "receipt")
               if getattr(args, n) is None]
    if missing:
        raise SystemExit("missing required arguments: "
                         + ", ".join("--" + m.replace("_", "-")
                                     for m in missing))

    frozen = {}
    for line in args.frozen.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            frozen[r["question_id"]] = r
    audit = [json.loads(l) for l in args.audit.read_text().splitlines()
             if l.strip()]
    errata_ids = [json.loads(l)["question_id"]
                  for l in args.errata.read_text().splitlines() if l.strip()]

    set_kept, set_excl = build_set_family(frozen, audit, args.db_root)
    cnt_kept, cnt_excl = build_count_family(frozen, errata_ids, args.db_root)
    items = sorted(set_kept + cnt_kept, key=lambda i: i["question_id"])

    pagination_failures = []
    for it in items:
        why = check_pagination(db_path(args.db_root, it["db_id"]), it)
        if why:
            pagination_failures.append(
                {"question_id": it["question_id"], "detail": why})

    pg_kept, pg_excl, pg_dbs = [], [], []
    if args.pg:
        pg_kept, pg_excl, pg_dbs, _uri = build_pg(
            items, args.db_root, args.pgdata)
        args.out_pg.write_text("".join(
            json.dumps(i, sort_keys=True) + "\n" for i in pg_kept))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("".join(
        json.dumps(i, sort_keys=True) + "\n"
        for i in items + [{**e, "excluded": True} for e in set_excl + cnt_excl]))

    receipt = {
        "probe": "established-interface truncation probe (D-142)",
        "freeze": str(FREEZE_PATH.relative_to(FREEZE_PATH.parents[2])),
        "freeze_sha256": freeze_sha256(),
        "k": K,
        "tool_call_budget": BUDGET,
        "sqlite_version": sqlite3.sqlite_version,
        "sqlglot_version": sqlglot.__version__,
        "universe": {
            "frozen_questions": str(args.frozen),
            "audit": str(args.audit),
            "errata": str(args.errata),
        },
        "families": {
            "SET": {
                "candidates": len(audit),
                "rule": "multiplicity_audit reference_rows > k",
                "eligible": len(set_kept),
                "excluded": len(set_excl),
            },
            "COUNT": {
                "candidates": len(errata_ids),
                "rule": "(a) counted domain derivable by the D-131 "
                        "projection-only rewrite and validated against "
                        "the database; (b) gold count > k",
                "eligible": len(cnt_kept),
                "excluded": len(cnt_excl),
            },
        },
        "eligible_total": len(items),
        "eligible_by_db": {d: sum(1 for i in items if i["db_id"] == d)
                           for d in sorted({i["db_id"] for i in items})},
        "N_stats": {
            "min": min(i["N"] for i in items),
            "max": max(i["N"] for i in items),
            "le_100": sum(1 for i in items if i["N"] <= 100),
            "gt_100": sum(1 for i in items if i["N"] > 100),
        },
        "max_page0_bytes": max(i["page0_bytes"] for i in items),
        "exclusions": sorted(set_excl + cnt_excl,
                             key=lambda e: (e["family"], e["question_id"])),
        "exclusion_counts": {
            r: sum(1 for e in set_excl + cnt_excl if e["reason"] == r)
            for r in sorted({e["reason"] for e in set_excl + cnt_excl})},
        "pagination_check": {
            "checked": len(items),
            "failures": pagination_failures,
        },
        "pg_leg": {
            "built": bool(args.pg),
            "databases": pg_dbs,
            "eligible": len(pg_kept),
            "excluded": len(pg_excl),
            "exclusions": pg_excl,
        },
        "manifest": str(args.out),
        "manifest_pg": str(args.out_pg) if args.pg else None,
    }
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps(receipt, indent=2, sort_keys=True)
                            + "\n")
    print(json.dumps({k: receipt[k] for k in
                      ("families", "eligible_total", "exclusion_counts",
                       "N_stats", "pg_leg")}, indent=2))
    if pagination_failures:
        print(f"PAGINATION CHECK FAILED on {len(pagination_failures)} items")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
