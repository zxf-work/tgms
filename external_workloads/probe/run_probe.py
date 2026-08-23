#!/usr/bin/env python
"""Established-interface truncation probe (D-142) — RUNNER.

The agent loop of PROBE_FREEZE.md, with no ECQR component anywhere.
The only tool is a REST-style list endpoint over a real database:

    list_records(page) -> at most k = 10 records of the question's
                          result list; a page past the end is empty.

Conditions differ ONLY in the metadata carried by the tool response:

    C0 bare   {"records": [...]}
    C1 flag   + "truncated": true|false      (true iff more pages exist)
    C2 total  + "truncated" and "total": N

The system prompt is byte-identical across conditions: it describes
the tool's mechanics and the answer schema and nothing else. Per the
freeze it gives NO guidance about counting strategy, pagination or
truncation, so it neither explains nor mentions the C1/C2 fields —
the metadata is in the response, not in the instructions, and exactly
one thing varies between conditions.

Budget: at most 10 tool calls per question; one re-prompt after a
malformed emission, after which the run is terminal (no_commitment).

    python external_workloads/probe/run_probe.py \
        --manifest external_workloads/probe/probe_manifest.jsonl \
        --db-root .../minidev/MINIDEV/dev_databases \
        --condition C0 --api http://127.0.0.1:8000/v1

    # engine-diversity leg (C0 only), served from embedded postgres
    python external_workloads/probe/run_probe.py --engine pg \
        --manifest external_workloads/probe/probe_manifest_pg.jsonl ...

    # validation without inference: scripted pseudo-agents
    python external_workloads/probe/run_probe.py --oracle enumerating ...
    python external_workloads/probe/run_probe.py --oracle diligent ...
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from setup_probe import (BUDGET, K, as_record, db_path,  # noqa: E402
                         execute_sql, freeze_sha256, load_manifest,
                         page_sql, pg_endpoint_table, _pgval)

MAX_NEW_TOKENS = 1024
MODEL = "Qwen/Qwen2.5-14B-Instruct-AWQ"
CONDITIONS = ("C0", "C1", "C2")

# --------------------------------------------------------------- prompts
# Tool mechanics + answer schema ONLY. Nothing here may hint at how to
# count, at paginating, or at truncation (PROBE_FREEZE.md).

SYSTEM_TEMPLATE = """You are answering a question about a database. \
You cannot query the database directly; you have one tool.

Tool: list_records(page) returns records from the result list for this \
question, 10 per page, starting at page 0.

To use the tool, reply with one fenced JSON block:
```json
{{"tool": "list_records", "page": 0}}
```
The tool's reply is a JSON object.

To answer, reply with one fenced JSON block:
```json
{answer_schema}
```

Every reply must be exactly one fenced JSON block: either a tool call \
or an answer.
"""

ANSWER_SCHEMA = {
    "COUNT": '{"count": <integer>}',
    "SET": '{"values": [<record>, <record>, ...]}',
}

USER_TEMPLATE = "Question: {question}\n"

RETRY_MESSAGE = ("That reply was not a single fenced JSON block. Reply "
                 "with exactly one fenced JSON block: either a tool call "
                 "or an answer.\n")


def system_prompt(family: str) -> str:
    return SYSTEM_TEMPLATE.format(answer_schema=ANSWER_SCHEMA[family])


def freeze_hashes() -> dict:
    return {
        "system_prompt_sha256": {
            f: hashlib.sha256(system_prompt(f).encode()).hexdigest()
            for f in ANSWER_SCHEMA},
        "user_template_sha256": hashlib.sha256(
            USER_TEMPLATE.encode()).hexdigest(),
        "retry_message_sha256": hashlib.sha256(
            RETRY_MESSAGE.encode()).hexdigest(),
        "k": K, "tool_call_budget": BUDGET,
        "malformed_retries": 1,
        "max_new_tokens": MAX_NEW_TOKENS, "model": MODEL,
        "temperature": 0.0, "seed": 0,
        "probe_freeze_sha256": freeze_sha256(),
    }


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


# --------------------------------------------------------------- parsing

FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)


def _json_candidates(text: str):
    for m in FENCE.finditer(text):
        yield m.group(1)
    yield text
    i, depth, start = 0, 0, None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                yield text[start:i + 1]


def parse_emission(text: str, family: str):
    """(kind, payload). kind in {'tool', 'answer', 'malformed'}.

    Lenient about surrounding prose and about which fence is used, and
    strict about the emission's own shape: a tool call names the tool
    and a zero-based integer page; an answer carries the key its family
    declares."""
    objs = []
    for cand in _json_candidates(text or ""):
        try:
            o = json.loads(cand)
        except Exception:
            continue
        if isinstance(o, dict):
            objs.append(o)
    for o in reversed(objs):          # a model that restates concludes last
        if o.get("tool") == "list_records" and "page" in o:
            p = _as_int(o["page"])
            if p is not None and p >= 0:
                return "tool", p
            return "malformed", None
        if family == "COUNT" and "count" in o:
            n = _as_int(o["count"])
            return ("answer", n) if n is not None else ("malformed", None)
        if family == "SET" and "values" in o:
            v = o["values"]
            return ("answer", v) if isinstance(v, list) else \
                   ("malformed", None)
    return "malformed", None


def _as_int(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str):
        try:
            return _as_int(json.loads(v.strip()))
        except Exception:
            return None
    return None


# --------------------------------------------------------------- endpoint

class SqliteEndpoint:
    engine = "sqlite"

    def __init__(self, db_root: Path):
        self.db_root = db_root

    def page(self, item: dict, p: int) -> list[dict]:
        rows, _c = execute_sql(db_path(self.db_root, item["db_id"]),
                               page_sql(item["endpoint_sql"], p))
        return [as_record(item["columns"], r) for r in rows]


class PgEndpoint:
    """The same endpoints, served from the embedded PostgreSQL built by
    setup_probe.py --pg: one ordered table per endpoint, paged with the
    engine's own LIMIT/OFFSET."""
    engine = "pg"

    def __init__(self, pgdata: Path, dsn: str | None = None):
        import psycopg2
        if dsn is None:
            import pgserver
            dsn = pgserver.get_server(pgdata, cleanup_mode=None).get_uri()
        self.conn = psycopg2.connect(dsn)
        self.conn.set_session(readonly=True, autocommit=True)

    def page(self, item: dict, p: int) -> list[dict]:
        cols = ", ".join(f'"{c}"' for c in item["columns"])
        cur = self.conn.cursor()
        cur.execute(f'SELECT {cols} FROM "{pg_endpoint_table(item["question_id"])}" '
                    f'ORDER BY _ord LIMIT {K} OFFSET {p * K}')
        rows = cur.fetchall()
        cur.close()
        return [as_record(item["columns"], [_pgval(v) for v in r])
                for r in rows]


def tool_response(records: list[dict], page: int, n: int,
                  condition: str) -> dict:
    body: dict = {"records": records}
    if condition in ("C1", "C2"):
        body["truncated"] = (page + 1) * K < n
    if condition == "C2":
        body["total"] = n
    return body


# --------------------------------------------------------------- oracles
# Scripted pseudo-agents. They emit through the SAME fenced-JSON
# channel and drive the SAME loop as the model, so a passing oracle run
# validates the whole plumbing (endpoint, paging, parsing, scoring)
# without any inference.

def oracle_emit(kind: str, state: dict) -> str:
    family, seen = state["family"], state["records_seen"]
    calls, last_n = state["calls_made"], state["last_page_len"]
    if kind == "enumerating":
        done = calls >= 1                       # reads page 0, answers
    elif kind == "diligent":
        done = calls >= BUDGET or last_n == 0   # empty page or budget
    else:
        raise SystemExit(f"unknown oracle {kind}")
    if not done:
        return ('```json\n' + json.dumps(
            {"tool": "list_records", "page": calls}) + '\n```')
    answer = ({"count": len(seen)} if family == "COUNT"
              else {"values": [r for _o, r in seen]})
    return '```json\n' + json.dumps(answer) + '\n```'


# ------------------------------------------------------------------- run

def run_item(item: dict, endpoint, condition: str, api: str,
             oracle: str | None) -> dict:
    family, n = item["family"], item["N"]
    messages = [{"role": "system", "content": system_prompt(family)},
                {"role": "user",
                 "content": USER_TEMPLATE.format(question=item["question"])}]
    seen: dict[int, dict] = {}          # absolute offset -> record
    calls: list[dict] = []
    malformed: list[str] = []
    retries_left = 1
    terminal, final, final_raw = None, None, None
    tokens = {"tokens_in": 0, "tokens_out": 0}
    last_page_len = None
    t0 = time.monotonic()

    while True:
        if oracle:
            content = oracle_emit(oracle, {
                "family": family, "calls_made": len(calls),
                "records_seen": sorted(seen.items()),
                "last_page_len": last_page_len})
            usage = {"tokens_in": 0, "tokens_out": 0}
        else:
            try:
                content, usage = chat(api, messages)
            except Exception as e:
                terminal = "api_error"
                final_raw = f"{type(e).__name__}: {e}"[:300]
                break
        tokens = {k: tokens[k] + usage.get(k, 0) for k in tokens}
        messages.append({"role": "assistant", "content": content})
        kind, payload = parse_emission(content, family)

        if kind == "malformed":
            malformed.append(content[:400])
            if retries_left > 0:
                retries_left -= 1
                messages.append({"role": "user", "content": RETRY_MESSAGE})
                continue
            terminal, final_raw = "no_commitment_malformed", content[:400]
            break

        if kind == "tool":
            if len(calls) >= BUDGET:
                # the budget is spent and the run never committed
                terminal, final_raw = "no_commitment_budget", content[:400]
                break
            recs = endpoint.page(item, payload)
            last_page_len = len(recs)
            for i, r in enumerate(recs):
                seen[payload * K + i] = r
            calls.append({"page": payload, "n_records": len(recs)})
            messages.append({"role": "user", "content": json.dumps(
                tool_response(recs, payload, n, condition),
                ensure_ascii=False)})
            continue

        terminal, final, final_raw = "final_answer", payload, content[:400]
        break

    npages = (n + K - 1) // K
    pages = {c["page"] for c in calls}
    return {
        "probe": "D-142", "engine": endpoint.engine, "condition": condition,
        "oracle": oracle, "question_id": item["question_id"],
        "family": family, "db_id": item["db_id"], "N": n, "k": K,
        "question": item["question"],
        "calls": calls, "n_calls": len(calls),
        "records_seen": [{"offset": o, "record": r}
                         for o, r in sorted(seen.items())],
        "seen": len(seen),
        "paginated_fully": set(range(npages)) <= pages,
        "final": final, "final_raw": final_raw, "terminal": terminal,
        "malformed_emissions": malformed,
        "tokens": tokens, "wall_s": round(time.monotonic() - t0, 3),
        "messages": messages,
    }


def main() -> int:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", type=Path,
                    default=here / "probe_manifest.jsonl")
    ap.add_argument("--db-root", type=Path)
    ap.add_argument("--condition", default="C0", choices=CONDITIONS)
    ap.add_argument("--engine", default="sqlite", choices=("sqlite", "pg"))
    ap.add_argument("--api", default="http://127.0.0.1:8000/v1")
    ap.add_argument("--runs-root", type=Path, default=here / "runs")
    ap.add_argument("--pgdata", type=Path,
                    default=here / "runs" / "pgdata")
    ap.add_argument("--pg-dsn", default=None)
    ap.add_argument("--oracle", default=None,
                    choices=("enumerating", "diligent"))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--print-freeze", action="store_true")
    args = ap.parse_args()

    if args.print_freeze:
        print(json.dumps(freeze_hashes(), indent=2, sort_keys=True))
        return 0
    if args.engine == "pg" and args.condition != "C0":
        raise SystemExit("the postgres leg is condition C0 only (freeze)")

    items = load_manifest(args.manifest)
    if args.limit:
        items = items[:args.limit]
    if args.engine == "pg":
        endpoint = PgEndpoint(args.pgdata, args.pg_dsn)
        cond_dir = "pg-" + args.condition
    else:
        if args.db_root is None:
            raise SystemExit("--db-root is required for the sqlite engine")
        endpoint = SqliteEndpoint(args.db_root)
        cond_dir = args.condition
    out_dir = args.runs_root / cond_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.monotonic()
    for i, item in enumerate(items, 1):
        rec = run_item(item, endpoint, args.condition, args.api, args.oracle)
        (out_dir / f"q{item['question_id']}.json").write_text(
            json.dumps(rec, indent=1, sort_keys=True) + "\n")
        print(f"[{i}/{len(items)}] q{item['question_id']} {item['family']} "
              f"N={item['N']} calls={rec['n_calls']} "
              f"seen={rec['seen']} {rec['terminal']}", flush=True)
    print(f"{len(items)} items -> {out_dir} in "
          f"{time.monotonic() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
