#!/usr/bin/env python
"""TopK over the BIRD Mini-Dev 500: replay the recorded agent SQL
through the top-k adapter against the pinned databases (no model calls).

For every question whose recorded agent statement carries an outer
LIMIT, the adapter parses ORDER BY / LIMIT (sqlglot), forms the
candidate domain Q' (outer LIMIT removed), decides `order_total` from
the schema (a unique non-null key of the single FROM table in the sort
list), re-executes the statement read-only, counts Q' on the same
database, builds the ranked-page descriptor and verifies the claim the
statement itself denotes: TopK(S = delivered rows, key/dir = its ORDER
BY, k = its LIMIT). The same replay runs over the gold SQL as a
secondary census of how BIRD writes rankings.

A data-level diagnostic accompanies each ranked statement: the ORDER BY
key values of the first min(k+1, |Q'|) candidates, from which it
records whether adjacent candidates tie (a boundary tie between rank k
and k+1 makes the top-k SET ambiguous; any tie inside the first k makes
the SEQUENCE ambiguous). It is a fact about the pinned database, never
an input to the verdict.

    PYTHONPATH=. python scripts/eval_bird_topk.py \
        --db-root /path/to/MINIDEV/dev_databases \
        --json benchmarks/results-v1/eval-bird-topk.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
import platform
import re
import sqlite3
import subprocess
from collections import Counter
from pathlib import Path

import sqlglot
from sqlglot import expressions as exp

from tgms.evidence.adapter_sql import (
    build_sql_topk_ecqr,
    parse_topk,
    sqlite_unique_keys,
)
from tgms.evidence.claims import TopK
from tgms.evidence.verify import Verdict, verify

ROOT = Path(__file__).resolve().parents[1]
BIRD = ROOT / "external_workloads" / "bird"


def _load_runner():
    """The agent runner's own executor and cell normalizer, so replayed
    rows are byte-identical to the recorded run's."""
    p = ROOT / "external_workloads" / "scripts" / "run_bird_agent.py"
    spec = importlib.util.spec_from_file_location("run_bird_agent", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def manifest_db_pins() -> dict[str, str]:
    text = (ROOT / "external_workloads" / "MANIFEST.yaml").read_text()
    block = text.split("database_sha256:", 1)[1]
    pins = {}
    for line in block.splitlines()[1:]:
        m = re.match(r'\s+(\w+):\s+"([0-9a-f]{64})"', line)
        if not m:
            break
        pins[m.group(1)] = m.group(2)
    return pins


def any_depth_ranked(sql: str) -> bool:
    try:
        t = sqlglot.parse_one(sql, read="sqlite")
    except Exception:  # noqa: BLE001
        return False
    return any(s.args.get("order") is not None
               and s.args.get("limit") is not None
               for s in t.find_all(exp.Select))


def outer_ranked(sql: str) -> bool:
    try:
        t = sqlglot.parse_one(sql, read="sqlite")
    except Exception:  # noqa: BLE001
        return False
    return t.args.get("order") is not None and t.args.get("limit") is not None


def key_probe_sql(sql: str, k: int) -> str | None:
    """Q' re-projected onto its ORDER BY keys, limited to k+1 rows.
    None when re-projection would change the candidate rows (DISTINCT,
    compound queries) or there is no ORDER BY."""
    try:
        t = sqlglot.parse_one(sql, read="sqlite")
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(t, exp.Select) or t.args.get("distinct"):
        return None
    order = t.args.get("order")
    if order is None:
        return None
    sel = list(t.expressions)
    aliases = {e.alias.lower(): e.this for e in sel
               if isinstance(e, exp.Alias)}
    keys = []
    for o in order.expressions:
        node = o.this
        if (isinstance(node, exp.Literal) and not node.is_string
                and str(node.this).isdigit()
                and 1 <= int(node.this) <= len(sel)):
            node = sel[int(node.this) - 1]
            node = node.this if isinstance(node, exp.Alias) else node
        elif (isinstance(node, exp.Column) and not node.table
              and node.name.lower() in aliases):
            node = aliases[node.name.lower()]
        keys.append(node.copy())
    probe = t.copy()
    # keys appended after the original projection, so aliases that
    # HAVING or ORDER BY reference stay bound; the probe reads only the
    # trailing key columns
    probe.set("expressions", [e.copy() for e in sel] + [
        exp.alias_(kx, f"_k{i}") for i, kx in enumerate(keys)])
    probe.set("order", exp.Order(expressions=[
        exp.Ordered(this=exp.column(f"_k{i}"),
                    desc=bool(o.args.get("desc")),
                    nulls_first=o.args.get("nulls_first"))
        for i, o in enumerate(order.expressions)]))
    probe.set("offset", None)
    probe.set("limit", exp.Limit(expression=exp.Literal.number(k + 1)))
    return probe.sql(dialect="sqlite")


def replay(runner, db: Path, sql: str, ukeys) -> dict:
    """Adapter + verifier over one recorded statement."""
    shape = parse_topk(sql, ukeys)
    rec = {"is_topk": shape.is_topk, "shape_reason": shape.reason,
           "limit": shape.limit, "order": shape.order,
           "order_total": shape.order_total,
           "unique_key": shape.unique_key}
    if not shape.is_topk:
        return rec
    try:
        rows, _cols = runner.execute_sql(db, sql)
        (crow,), _c = runner.execute_sql(
            db, f"SELECT COUNT(*) FROM ({shape.candidate_sql}) t")
        n_cand = runner.integral(crow[0])
    except Exception as e:  # noqa: BLE001
        rec["exec_error"] = f"{type(e).__name__}: {e}"[:300]
        return rec
    e = build_sql_topk_ecqr(rows=rows, sql=sql, shape=shape,
                            store_id=f"bird:{db.stem}", engine="sqlite",
                            engine_version=sqlite3.sqlite_version,
                            candidate_count=n_cand)
    claim = TopK(rows=rows, key=[p[0] for p in shape.order],
                 dir=[p[1] for p in shape.order], k=shape.limit)
    j = verify(claim, e, {"rows": rows})
    rec.update({"n_rows": len(rows), "n_candidates": n_cand,
                "delivery_complete": e.scope.delivery_complete,
                "verdict": j.verdict.value, "verdict_reason": j.reason})
    probe = key_probe_sql(shape.candidate_sql, shape.limit)
    if probe is None:
        rec["tie_probe"] = "unavailable"
        return rec
    try:
        keys, _c = runner.execute_sql(db, probe)
    except Exception as e:  # noqa: BLE001
        rec["tie_probe"] = f"error: {type(e).__name__}"
        return rec
    k, nk = shape.limit, len(shape.order)
    keys = [r[-nk:] for r in keys]
    adj = [keys[i] == keys[i + 1] for i in range(len(keys) - 1)]
    rec["tie_probe"] = "ok"
    rec["tie_inside_top_k"] = any(adj[:max(0, min(k, len(keys)) - 1)])
    rec["tie_at_boundary"] = len(keys) > k and adj[k - 1]
    return rec


def summarize(recs: list[dict]) -> dict:
    topk = [r for r in recs if r.get("is_topk")]
    ran = [r for r in topk if "verdict" in r]
    sup = [r for r in ran if r["verdict"] == Verdict.SUPPORTED.value]
    refused = [r for r in ran if r["verdict"] != Verdict.SUPPORTED.value]
    not_total = [r for r in refused
                 if r["verdict"] == "UNSUPPORTED_ORDER_NOT_TOTAL"]
    probed = [r for r in not_total if r.get("tie_probe") == "ok"]
    return {
        "n_outer_limit": sum(1 for r in recs if r.get("is_topk")
                             or r.get("shape_reason") in (
                                 "outer_offset",
                                 "non_literal_or_zero_limit")),
        "n_outer_offset": sum(1 for r in recs
                              if r.get("shape_reason") == "outer_offset"),
        "n_topk_shaped": len(topk),
        "n_topk_with_order_by": sum(1 for r in topk if r["order"]),
        "n_exec_error": sum(1 for r in topk if "exec_error" in r),
        "n_topk_certified": len(sup),
        "verdicts": dict(Counter(r["verdict"] for r in ran)),
        "order_not_total_by_reason": dict(Counter(
            r["shape_reason"] for r in not_total)),
        "certified_by_reason": dict(Counter(
            r["shape_reason"] for r in sup)),
        "certified_full_page": sum(1 for r in sup
                                   if r["n_rows"] == r["limit"]),
        "certified_exhausted": sum(1 for r in sup
                                   if r["n_rows"] < r["limit"]),
        "order_not_total_tie_probe": {
            "probed": len(probed),
            "unavailable": len(not_total) - len(probed),
            "tie_at_boundary": sum(1 for r in probed
                                   if r["tie_at_boundary"]),
            "tie_inside_top_k_only": sum(
                1 for r in probed if r["tie_inside_top_k"]
                and not r["tie_at_boundary"]),
            "no_tie_in_first_k_plus_1": sum(
                1 for r in probed if not r["tie_inside_top_k"]
                and not r["tie_at_boundary"]),
        },
        "certified_with_observed_tie": sum(
            1 for r in sup if r.get("tie_probe") == "ok"
            and (r["tie_inside_top_k"] or r["tie_at_boundary"])),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-root", type=Path, required=True)
    ap.add_argument("--json", type=Path, required=True)
    args = ap.parse_args()
    if args.json.exists():
        raise SystemExit(f"{args.json} exists; receipts are records")
    runner = _load_runner()

    frozen = [json.loads(line) for line in
              open(BIRD / "bird_500_select_sqlite.jsonl")]
    agent = {}
    for p in sorted((BIRD / "agent_run").glob("q*.json")):
        it = json.loads(p.read_text())
        agent[it["question_id"]] = it
    adj = {json.loads(line)["question_id"]: json.loads(line)
           for line in open(BIRD / "adjudication_final.jsonl")}
    sem = {json.loads(line)["question_id"]:
           json.loads(line)["semantic_property"]
           for line in open(BIRD / "claim_annotation.jsonl")}

    pins = manifest_db_pins()
    dbs = {}
    for db_id in sorted({r["db_id"] for r in frozen}):
        p = args.db_root / db_id / f"{db_id}.sqlite"
        got = sha256_file(p)
        if got != pins.get(db_id):
            raise SystemExit(f"{db_id}: sha256 {got} != manifest pin")
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            dbs[db_id] = (p, sqlite_unique_keys(con))
        finally:
            con.close()

    items = []
    for rec in frozen:
        q, db_id = rec["question_id"], rec["db_id"]
        db, ukeys = dbs[db_id]
        it = agent.get(q, {})
        a = adj.get(q, {})
        item = {
            "question_id": q, "db_id": db_id,
            "semantic_property": sem.get(q),
            "adjudicated_properties": a.get("semantic_properties"),
            "gold_ranked_any_depth": any_depth_ranked(rec["gold_sql"]),
            "gold_ranked_outer": outer_ranked(rec["gold_sql"]),
            "agent_stage": it.get("stage"), "agent_em": it.get("em"),
            "claim_form": it.get("claim_form"),
            "full_question_contract_covered": it.get(
                "full_question_contract_covered"),
            "gold": replay(runner, db, rec["gold_sql"], ukeys),
        }
        if it.get("sql"):
            item["agent_ranked_outer"] = outer_ranked(it["sql"])
            item["agent"] = replay(runner, db, it["sql"], ukeys)
        items.append(item)

    agent_recs = [i["agent"] for i in items if "agent" in i]
    gold_recs = [i["gold"] for i in items]
    gold96 = [i for i in items if i["gold_ranked_any_depth"]]
    g96_agent = [i["agent"] for i in gold96 if "agent" in i]

    # the strict full-contract count (eval-bird-agent.json:
    # certified_full_contract) changes only for an item that is
    # certified, not covered, not duplicate-bearing, and whose missing
    # part is an ordering TopK can certify
    prior = json.loads((ROOT / "benchmarks" / "results-v1" /
                        "eval-bird-agent.json").read_text())
    candidates = []
    for i in items:
        it = agent.get(i["question_id"], {})
        if it.get("stage") != "certified" or it.get(
                "full_question_contract_covered", True):
            continue
        if it.get("duplicate_free_reference") is False:
            continue
        a = adj.get(i["question_id"], {})
        ag = i.get("agent", {})
        candidates.append({
            "question_id": i["question_id"],
            "adjudicated_properties": a.get("semantic_properties"),
            "mismatch_kind": a.get("mismatch_kind"),
            "agent_topk_shaped": ag.get("is_topk", False),
            "topk_verdict": ag.get("verdict"),
            "shape_reason": ag.get("shape_reason"),
            "ordering_is_the_missing_part": "ORDERED_TOP_K" in (
                a.get("semantic_properties") or []),
            "would_cover_full_contract": (
                "ORDERED_TOP_K" in (a.get("semantic_properties") or [])
                and ag.get("verdict") == Verdict.SUPPORTED.value),
        })
    gained = sum(c["would_cover_full_contract"] for c in candidates)

    commit = os.environ.get("COMMIT") or subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        capture_output=True, text=True).stdout.strip()
    out = {
        "experiment": "TopK certification over the BIRD Mini-Dev 500 "
                      "(replay of recorded agent SQL; no model calls)",
        "commit": commit,
        "host": platform.node(),
        "slurm_job": os.environ.get("SLURM_JOB_ID"),
        "date_utc": dt.datetime.now(dt.timezone.utc).isoformat(
            timespec="seconds"),
        "protocol": {
            "agent_sql_source": "external_workloads/bird/agent_run/"
                                "q*.json (field sql; run 185542, "
                                "recorded in eval-bird-agent.json)",
            "gold_sql_source": "external_workloads/bird/"
                               "bird_500_select_sqlite.jsonl",
            "database_sha256_checked_against": "external_workloads/"
                                               "MANIFEST.yaml",
            "sqlite_version": sqlite3.sqlite_version,
            "sqlglot_version": sqlglot.__version__,
            "execution": "read-only, run_bird_agent.execute_sql "
                         "(600 s ceiling), cells normalized by its "
                         "norm_cell",
            "claim": "TopK(S = delivered rows, key/dir = the statement's "
                     "ORDER BY as written, k = its LIMIT)",
            "candidate_domain": "the statement with its outer LIMIT "
                                "removed (sqlglot, sqlite dialect); "
                                "inner LIMITs kept",
            "order_total_rule": "the ORDER BY list contains every column "
                                "of a unique non-null key of the single "
                                "base table in FROM: rowid/_rowid_/oid on "
                                "rowid tables, or the PRIMARY KEY when it "
                                "cannot hold NULL (INTEGER PRIMARY KEY, "
                                "WITHOUT ROWID, or all key columns NOT "
                                "NULL); joins, GROUP BY, derived sources, "
                                "compound queries and DISTINCT over an "
                                "unprojected key are not established",
            "delivery_complete": "page shorter than k (candidates "
                                 "exhausted) or COUNT(*) of Q' equals "
                                 "the page length",
            "outer_offset": "LIMIT with nonzero OFFSET is not a top-k "
                            "page (a rank window) and is not instantiated",
            "tie_probe": "diagnostic only: Q' re-projected onto its ORDER "
                         "BY keys, LIMIT k+1; adjacent equal key tuples "
                         "are ties",
        },
        "agent": summarize(agent_recs),
        "agent_on_gold_ranked_any_depth": {
            "n_gold_ranked_any_depth": len(gold96),
            "n_gold_ranked_outer": sum(i["gold_ranked_outer"]
                                       for i in items),
            "n_agent_sql": len(g96_agent),
            **summarize(g96_agent)},
        "gold": summarize(gold_recs),
        "strict_full_contract": {
            "before": prior.get("certified_full_contract"),
            "partial_contract_reasons_before": prior.get(
                "partial_contract_reasons"),
            "non_duplicate_partial_items": candidates,
            "gained_with_topk": gained,
            "after": (prior.get("certified_full_contract") or 0) + gained,
        },
        "items": items,
    }
    args.json.write_text(json.dumps(out, indent=1, sort_keys=False) + "\n")
    print(json.dumps({k: out[k] for k in (
        "agent", "agent_on_gold_ranked_any_depth", "gold",
        "strict_full_contract")}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
