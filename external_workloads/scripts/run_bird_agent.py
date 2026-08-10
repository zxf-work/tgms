#!/usr/bin/env python
"""BIRD Mini-Dev 500 agent arm (D-130): frozen single-configuration
coverage study of the ECQR contract on an established external
benchmark.

System boundary (FROZEN in external_workloads/FREEZE.md before the
first run; the prompt-template hash printed by --print-freeze is
recorded there):

  * one model configuration (Qwen2.5-14B-Instruct-AWQ, vLLM 0.11.0,
    OpenAI-compatible endpoint), temperature 0, one seed;
  * schema prompt = the database's verbatim CREATE statements +
    question + BIRD external-knowledge evidence string;
  * at most REPAIR_ROUNDS repair rounds, each fed the sqlite error
    text of the failed attempt, nothing else;
  * agent SQL executes read-only on the pinned sqlite database with
    the same non-tunable 600 s ceiling as gold validation;
  * the claim CONSTRUCTOR applies the predeclared per-question claim
    form (claim_annotation.jsonl + adjudication) to the agent's OWN
    executed result — semantic typed claims, a declared departure
    from the SNAP arms' SQL-conservative constructor;
  * EM is order-insensitive SET equality of result rows against the
    re-executed gold (the official BIRD evaluator's comparison).

Constructor rules per predeclared form (agent result shape must match,
else the item exits the funnel at `shape_mismatch`):

  SCALAR        1 row x 1 col   -> Scalar(path="rows[0][0]")
  SCALAR_TUPLE  1 row x >=1 col -> one Scalar per column
  EXACT_COUNT   1 row x 1 col integer-valued, outer projection is a
                single aggregate, no outer LIMIT
                -> ExactCount(n); the completed unlimited count
                   execution IS the cardinality certificate
                   (exact_cardinality = n), per FREEZE.md
  COMPLETE_SET  any row count -> CompleteSet(members = canonical-JSON
                row serializations; set semantics per rule R3)

Evidence: build_sql_ecqr(engine="sqlite"); for non-count forms
total_count = SELECT COUNT(*) FROM (<agent sql>) t over the FULL
semantic query (an agent LIMIT is semantic under Interpretation 1,
so the certificate wraps it and the delivered page equals the count).

    python external_workloads/scripts/run_bird_agent.py \
        --frozen external_workloads/bird/bird_500_select_sqlite.jsonl \
        --annotation external_workloads/bird/claim_annotation.jsonl \
        --adjudication external_workloads/bird/adjudication_proposed.jsonl \
        --db-root /path/to/minidev/dev_databases \
        --api http://127.0.0.1:8000/v1 \
        --out external_workloads/bird/agent_run \
        [--limit N] [--print-freeze]
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import platform
import re
import sqlite3
import time
import urllib.request
from pathlib import Path

import sqlglot
from sqlglot import expressions as exp

from tgms.core.model import canonical_json
from tgms.evidence.adapter_sql import build_sql_ecqr
from tgms.evidence.claims import (CompleteSet, ExactCount,
                                  Existence, Scalar)
from tgms.evidence.verify import Verdict, verify

CEILING_S = 600            # non-tunable; FREEZE.md gold-validation ceiling
REPAIR_ROUNDS = 3          # max repair rounds after the first attempt
MAX_NEW_TOKENS = 1024
MODEL = "Qwen/Qwen2.5-14B-Instruct-AWQ"

PROMPT_TEMPLATE = """You are a careful SQLite analyst. Write ONE SQLite \
SELECT statement that answers the question over the schema below. \
Output only the SQL inside a ```sql fence, with no commentary.

Database schema:
{schema}

External knowledge (may be empty):
{evidence}

Question: {question}
"""

REPAIR_TEMPLATE = """Your SQL failed to execute.

Error: {error}

Previous SQL:
```sql
{sql}
```

Write ONE corrected SQLite SELECT statement for the same question. \
Output only the SQL inside a ```sql fence, with no commentary.
"""


def freeze_hashes() -> dict:
    return {
        "prompt_template_sha256": hashlib.sha256(
            PROMPT_TEMPLATE.encode()).hexdigest(),
        "repair_template_sha256": hashlib.sha256(
            REPAIR_TEMPLATE.encode()).hexdigest(),
        "repair_rounds": REPAIR_ROUNDS,
        "ceiling_s": CEILING_S,
        "max_new_tokens": MAX_NEW_TOKENS,
        "model": MODEL,
        "temperature": 0.0,
        "seed": 0,
    }


# ---------------------------------------------------------------- sqlite

def schema_dump(db: Path) -> str:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
    finally:
        con.close()
    return "\n\n".join(r[0] for r in rows if r[0])


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


# ----------------------------------------------------------------- model

def chat(api: str, messages: list[dict]) -> tuple[str, dict]:
    body = json.dumps({
        "model": MODEL, "messages": messages, "temperature": 0.0,
        "seed": 0, "max_tokens": MAX_NEW_TOKENS,
    }).encode()
    req = urllib.request.Request(
        api.rstrip("/") + "/chat/completions", data=body,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as resp:
        out = json.loads(resp.read())
    usage = out.get("usage", {})
    return out["choices"][0]["message"]["content"], {
        "tokens_in": usage.get("prompt_tokens", 0),
        "tokens_out": usage.get("completion_tokens", 0)}


FENCE = re.compile(r"```sql\s*(.*?)```", re.S | re.I)


def extract_sql(text: str) -> str | None:
    m = FENCE.search(text)
    sql = m.group(1) if m else None
    if sql is None:
        i = text.upper().find("SELECT")
        sql = text[i:] if i >= 0 else None
    if sql is None:
        return None
    sql = sql.strip().rstrip(";").strip()
    if not sql or not re.match(r"(?is)^\s*(select|with)\b", sql):
        return None
    return sql


# ----------------------------------------------------------- constructor

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


def boolean_cell(rows) -> bool:
    if len(rows) != 1 or len(rows[0]) != 1:
        return False
    v = rows[0][0]
    if isinstance(v, bool):
        return True
    if isinstance(v, (int, float)) and v in (0, 1):
        return True
    return isinstance(v, str) and v.strip().upper() in (
        "YES", "NO", "TRUE", "FALSE", "Y", "N")


def construct(form: str, rows: list, sql: str):
    """(claims, exit_reason). The predeclared per-question encoding is
    applied to the agent's own executed result; a shape divergence
    exits the funnel rather than re-fitting the form.

    ExactCount(n) is emitted ONLY where the descriptor's own result
    has cardinality n (D-132 rule 1). A count-VALUED aggregate is a
    Scalar: `SELECT COUNT(*) ...` has |R*(Q,B)| = 1 whatever number
    that single row holds.
    """
    if form == "OUTSIDE_FRAGMENT":
        return None, "outside_fragment"
    if form == "SCALAR":
        if len(rows) != 1 or len(rows[0]) != 1:
            return None, "shape_mismatch"
        return [Scalar(path="rows[0][0]", value=rows[0][0])], None
    if form == "SCALAR_TUPLE":
        if len(rows) != 1 or not rows[0]:
            return None, "shape_mismatch"
        return [Scalar(path=f"rows[0][{k}]", value=v)
                for k, v in enumerate(rows[0])], None
    if form == "COMPLETE_SET":
        return [CompleteSet(
            members=[canonical_json(r) for r in rows])], None
    if form == "EXISTS":
        # the claim is about witnesses, so a computed truth value is
        # not the requested answer shape
        if boolean_cell(rows):
            return None, "shape_mismatch"
        return [Existence()], None
    if form == "SET_AND_COUNT":
        return [CompleteSet(members=[canonical_json(r) for r in rows]),
                ExactCount(n=len(rows))], None
    return None, "unknown_form"


def has_outer_limit(sql: str) -> bool:
    try:
        tree = sqlglot.parse_one(sql, read="sqlite")
    except Exception:
        return False
    return tree.args.get("limit") is not None


# ------------------------------------------------------------------ main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frozen", type=Path, required=True)
    ap.add_argument("--annotation", type=Path, required=True)
    ap.add_argument("--adjudication", type=Path, required=True)
    ap.add_argument("--errata", type=Path, required=True)
    ap.add_argument("--multiplicity", type=Path, default=None,
                    help="bag-vs-set audit; duplicate-bearing reference "
                         "results mark the full contract uncovered")
    ap.add_argument("--db-root", type=Path, required=True)
    ap.add_argument("--api", default="http://127.0.0.1:8000/v1")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--print-freeze", action="store_true")
    ap.add_argument("--replay-from", type=Path, default=None,
                    help="re-run the claim constructor, adapter and "
                         "verifier over the SQL already recorded in a "
                         "completed run directory; no model inference")
    args = ap.parse_args()

    if args.print_freeze:
        print(json.dumps(freeze_hashes(), indent=1))
        return 0

    frozen = [json.loads(l) for l in open(args.frozen)]
    forms = {json.loads(l)["question_id"]: json.loads(l)["auto_claim"]
             for l in open(args.annotation)}
    sem = {json.loads(l)["question_id"]: json.loads(l)["semantic_property"]
           for l in open(args.annotation)}
    # rule-1 errata first (count-VALUED aggregates are Scalars), then
    # the PI-adjudicated encodings for the queue
    for l in open(args.errata):
        r = json.loads(l)
        assert forms[r["question_id"]] == r["from"]
        forms[r["question_id"]] = r["to"]
    ENC = {"Scalar": "SCALAR", "Scalar (Boolean)": "SCALAR",
           "Scalar bundle over one row": "SCALAR_TUPLE",
           "CompleteSet": "COMPLETE_SET",
           "CompleteSet (tuple projection)": "COMPLETE_SET",
           "CompleteSet (unordered projection)": "COMPLETE_SET",
           "Existence": "EXISTS",
           "CompleteSet + ExactCount": "SET_AND_COUNT",
           "OUTSIDE_CURRENT_FRAGMENT": "OUTSIDE_FRAGMENT"}
    adj = {}
    for l in open(args.adjudication):
        r = json.loads(l)
        forms[r["question_id"]] = ENC[r["ecqr_encoding"]]
        adj[r["question_id"]] = r
    assert all(f in {"SCALAR", "SCALAR_TUPLE", "COMPLETE_SET", "EXISTS",
                     "SET_AND_COUNT", "OUTSIDE_FRAGMENT"}
               for f in forms.values()), set(forms.values())

    dupfree = {}
    if args.multiplicity is not None and args.multiplicity.exists():
        for l in open(args.multiplicity):
            r = json.loads(l)
            dupfree[r["question_id"]] = r["duplicate_free_reference"]

    args.out.mkdir(parents=True, exist_ok=True)
    schemas: dict[str, str] = {}
    n_done = 0

    for rec in frozen:
        if args.limit and n_done >= args.limit:
            break
        qid, db_id = rec["question_id"], rec["db_id"]
        out_path = args.out / f"q{qid}.json"
        if out_path.exists():
            n_done += 1
            continue
        db = args.db_root / db_id / f"{db_id}.sqlite"
        if db_id not in schemas:
            schemas[db_id] = schema_dump(db)
        a = adj.get(qid, {})
        item = {"question_id": qid, "db_id": db_id,
                "difficulty": rec.get("difficulty"),
                "claim_form": forms[qid],
                "semantic_property": sem.get(qid),
                "question_gold_mismatch": a.get(
                    "question_gold_mismatch", False),
                "full_question_contract_covered": (
                    a.get("full_question_contract_covered", True)
                    and dupfree.get(qid, True)),
                "duplicate_free_reference": dupfree.get(qid),
                "tokens_in": 0, "tokens_out": 0, "attempts": []}
        t_start = time.monotonic()

        rows = cols = sql = None
        if args.replay_from is not None:
            prior_p = args.replay_from / f"q{qid}.json"
            if not prior_p.exists():
                continue
            prior = json.loads(prior_p.read_text())
            item.update({"tokens_in": prior["tokens_in"],
                         "tokens_out": prior["tokens_out"],
                         "attempts": prior["attempts"],
                         "replayed_from": str(args.replay_from)})
            if prior.get("sql"):
                sql, cols_ = prior["sql"], None
                rows, cols = execute_sql(db, sql)
                del cols_
        else:
            messages = [{"role": "user", "content": PROMPT_TEMPLATE.format(
                schema=schemas[db_id], evidence=rec.get("evidence") or "",
                question=rec["question"])}]
            for attempt in range(1 + REPAIR_ROUNDS):
                try:
                    text, usage = chat(args.api, messages)
                except Exception as e:  # endpoint failure is a run error
                    raise SystemExit(f"model endpoint failure at q{qid}: {e}")
                item["tokens_in"] += usage["tokens_in"]
                item["tokens_out"] += usage["tokens_out"]
                cand = extract_sql(text)
                if cand is None:
                    err = "no SQL SELECT statement found in the reply"
                else:
                    try:
                        t0 = time.monotonic()
                        rows, cols = execute_sql(db, cand)
                        item["attempts"].append(
                            {"n": attempt, "ok": True,
                             "exec_s": round(time.monotonic() - t0, 3)})
                        sql = cand
                        break
                    except Exception as e:
                        err = f"{type(e).__name__}: {e}"[:400]
                item["attempts"].append({"n": attempt, "ok": False,
                                         "error": err})
                messages += [
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": REPAIR_TEMPLATE.format(
                        error=err, sql=cand or "(none)")}]

        if sql is None:
            item["stage"] = "no_executable_sql"
        else:
            item.update({"sql": sql, "n_rows": len(rows),
                         "n_cols": len(cols)})
            claims, exit_reason = construct(forms[qid], rows, sql)
            if claims is None:
                item["stage"] = exit_reason
            else:
                # evidence descriptor over the completed execution
                limited = has_outer_limit(sql)
                total = None
                if limited:
                    # a completed unlimited statement delivers its
                    # whole result; when the semantic query carries an
                    # outer LIMIT (semantic under Interpretation 1) the
                    # delivered page is complete only if it equals the
                    # count of the FULL semantic query
                    try:
                        (crows, _c) = execute_sql(
                            db, f"SELECT COUNT(*) FROM ({sql}) t")
                        total = integral(crows[0][0])
                    except Exception as e:
                        item["certificate_error"] = \
                            f"{type(e).__name__}: {e}"[:200]
                ser_rows = ([canonical_json(r) for r in rows]
                            if forms[qid] in ("COMPLETE_SET",
                                              "SET_AND_COUNT")
                            else rows)
                item["claim_kinds"] = [c.kind for c in claims]
                evidence = build_sql_ecqr(
                    rows=ser_rows, sql=sql,
                    store_id=f"bird:{db_id}",
                    engine="sqlite",
                    engine_version=sqlite3.sqlite_version,
                    total_count=total, limited=limited)
                result = {"rows": ser_rows}
                item["descriptor_bytes"] = len(canonical_json({
                    "claims": [c.__dict__ for c in claims],
                    "ecqr": {"result_id": evidence.result_id,
                             "scope": evidence.scope.__dict__,
                             "basis": evidence.basis.__dict__}}))
                verdicts = [verify(c, evidence, result) for c in claims]
                item["verdicts"] = [
                    {"kind": c.kind, "verdict": j.verdict.value,
                     "reason": j.reason}
                    for c, j in zip(claims, verdicts)]
                certified = all(j.verdict is Verdict.SUPPORTED
                                for j in verdicts)
                item["stage"] = ("certified" if certified
                                 else "uncertified")

            # EM regardless of certification: re-execute gold, compare
            # row SETS order-insensitively (official BIRD comparison)
            try:
                grows, _gc = execute_sql(db, rec["gold_sql"])
                item["em"] = ({tuple(json.dumps(c, sort_keys=True)
                                     for c in r) for r in rows}
                              == {tuple(json.dumps(c, sort_keys=True)
                                        for c in r) for r in grows})
            except Exception as e:
                item["em"] = None
                item["gold_error"] = f"{type(e).__name__}: {e}"[:200]

        item["wall_s"] = round(time.monotonic() - t_start, 3)
        out_path.write_text(json.dumps(item, sort_keys=True) + "\n")
        n_done += 1
        print(f"[{n_done}] q{qid} {db_id} -> {item['stage']}"
              f" em={item.get('em')}", flush=True)

    # ------------------------------------------------------- aggregate
    items = [json.loads((args.out / f"q{r['question_id']}.json")
                        .read_text()) for r in frozen
             if (args.out / f"q{r['question_id']}.json").exists()]
    stages, by_form, reasons = {}, {}, {}
    med = sorted(it["descriptor_bytes"] for it in items
                 if "descriptor_bytes" in it)
    for it in items:
        stages[it["stage"]] = stages.get(it["stage"], 0) + 1
        f = by_form.setdefault(it["claim_form"], {
            "n": 0, "executable": 0, "claim_constructed": 0,
            "certified": 0, "certified_and_correct": 0, "em": 0})
        f["n"] += 1
        if it["stage"] != "no_executable_sql":
            f["executable"] += 1
        if it["stage"] in ("certified", "uncertified"):
            f["claim_constructed"] += 1
        if it["stage"] == "certified":
            f["certified"] += 1
            if it.get("em"):
                f["certified_and_correct"] += 1
        if it.get("em"):
            f["em"] += 1
        for v in it.get("verdicts", []):
            if v["verdict"] != "SUPPORTED":
                reasons[v["verdict"]] = reasons.get(v["verdict"], 0) + 1
    receipt = {
        "host": platform.node(), "n": len(items),
        "freeze": freeze_hashes(),
        "stage_counts": stages,
        "by_form": by_form,
        "unsupported_verdict_counts": reasons,
        "funnel": {
            "universe": len(items),
            "executable_sql": sum(
                it["stage"] != "no_executable_sql" for it in items),
            "claim_constructed": sum(
                it["stage"] in ("certified", "uncertified")
                for it in items),
            "certified": sum(
                it["stage"] == "certified" for it in items),
            "certified_and_correct": sum(
                it["stage"] == "certified" and it.get("em")
                for it in items),
        },
        "em_overall": sum(bool(it.get("em")) for it in items),
        "certified": sum(it["stage"] == "certified" for it in items),
        "certified_and_correct": sum(
            it["stage"] == "certified" and it.get("em") for it in items),
        "outside_fragment": sum(
            it["stage"] == "outside_fragment" for it in items),
        "question_gold_mismatch": sum(
            1 for it in items if it.get("question_gold_mismatch")),
        "full_contract_not_covered": sum(
            1 for it in items
            if not it.get("full_question_contract_covered", True)),
        "duplicate_bearing_reference": sum(
            1 for it in items if it.get("duplicate_free_reference") is False),
        "certified_full_contract": sum(
            1 for it in items if it["stage"] == "certified"
            and it.get("full_question_contract_covered", True)),
        "claim_kind_census": {
            k: sum(it.get("claim_kinds", []).count(k) for it in items)
            for k in ("scalar", "exact_count", "complete_set")},
        "tokens_in_total": sum(it["tokens_in"] for it in items),
        "tokens_out_total": sum(it["tokens_out"] for it in items),
        "descriptor_bytes_median": (med[len(med) // 2] if med else None),
        "repair_used": sum(len(it["attempts"]) > 1 for it in items),
        "sqlite_version": sqlite3.sqlite_version,
    }
    (args.out / "receipt.json").write_text(
        json.dumps(receipt, indent=1) + "\n")
    print(json.dumps(receipt, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
