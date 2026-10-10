#!/usr/bin/env python
"""Truncation probe under ECQR gating (D-170, lane D): RUNNER.

PROBE_ECQR_FREEZE.md. The probe's agent loop, endpoint, parser and
budget (run_probe.py, setup_probe.py) are imported unchanged; what
this leg adds is the evidence path around them:

  * every tool reply passes through the SQL evidence adapter
    (`build_sql_ecqr`, pagination-aware: limited=True, total_count=N
    from an unlimited count over the endpoint query), and the model
    sees the descriptor's scope fields beside the records;
  * at the final answer the records at every distinct retrieved
    offset form the assembled step s*, described by the same adapter;
  * the answer becomes typed claims (ExactCount for COUNT; CompleteSet
    plus one Membership per distinct answered record for SET), the
    generic verifier judges each against s*, and a deterministic
    renderer states only supported claims, abstaining otherwise.

Conditions:
    E         probe system prompt, byte-identical to C0..C2
    E-aware   + the frozen paragraph explaining the evidence and gate
    C0-replay run_probe.run_item, condition C0, unmodified (cost
              reference: the committed C0 transcripts are not retained)

    python external_workloads/probe/run_probe_ecqr.py \
        --db-root .../minidev/MINIDEV/dev_databases \
        --condition E --api http://127.0.0.1:8000/v1

    # validation without inference: scripted pseudo-agents, condition E
    python external_workloads/probe/run_probe_ecqr.py --oracle page_only ...
    python external_workloads/probe/run_probe_ecqr.py --oracle enumerating ...
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_probe import (MAX_NEW_TOKENS, MODEL, RETRY_MESSAGE,  # noqa: E402
                       USER_TEMPLATE, SqliteEndpoint, chat, parse_emission,
                       run_item, system_prompt)
from setup_probe import (BUDGET, K, canon, db_path, execute_sql,  # noqa: E402
                         freeze_sha256, load_manifest)

from tgms.evidence.adapter_sql import build_sql_ecqr  # noqa: E402
from tgms.evidence.claims import (CompleteSet, ExactCount,  # noqa: E402
                                  Membership)
from tgms.evidence.verify import Verdict, verify  # noqa: E402

HERE = Path(__file__).resolve().parent
ECQR_FREEZE_PATH = HERE / "PROBE_ECQR_FREEZE.md"
CONDITIONS = ("E", "E-aware", "C0-replay")
OUTCOMES = ("certified", "withheld", "abstained", "error")

# Appended verbatim (one blank line between) after the probe's system
# prompt for E-aware; must appear verbatim in PROBE_ECQR_FREEZE.md.
AWARE_ADDENDUM = (
    'Each tool reply also carries an "evidence" object. '
    '"exact_cardinality" is the certified total number of records in the '
    'full result list for this question. "delivery_complete" is false '
    'when the records in that reply are not the whole list.\n'
    '\n'
    'Your answer is checked against this evidence before it is shown to '
    'the user. A count is shown only if it equals the certified total. A '
    'list of values is shown as the complete answer only if every record '
    'of the result list has been retrieved; otherwise it is withheld, and '
    'only the listed records that were actually retrieved are shown, '
    'marked as a partial list.')


def ecqr_freeze_sha256() -> str:
    return hashlib.sha256(ECQR_FREEZE_PATH.read_bytes()).hexdigest()


def check_freeze_text() -> None:
    if AWARE_ADDENDUM not in ECQR_FREEZE_PATH.read_text():
        raise SystemExit("AWARE_ADDENDUM is not verbatim in "
                         "PROBE_ECQR_FREEZE.md; refusing to run")


def system_prompt_for(condition: str, family: str) -> str:
    base = system_prompt(family)
    if condition == "E-aware":
        return base + "\n" + AWARE_ADDENDUM + "\n"
    return base


def freeze_hashes() -> dict:
    return {
        "system_prompt_sha256": {
            c: {f: hashlib.sha256(system_prompt_for(c, f).encode())
                .hexdigest() for f in ("COUNT", "SET")}
            for c in ("E", "E-aware")},
        "aware_addendum_sha256": hashlib.sha256(
            AWARE_ADDENDUM.encode()).hexdigest(),
        "user_template_sha256": hashlib.sha256(
            USER_TEMPLATE.encode()).hexdigest(),
        "retry_message_sha256": hashlib.sha256(
            RETRY_MESSAGE.encode()).hexdigest(),
        "k": K, "tool_call_budget": BUDGET, "malformed_retries": 1,
        "max_new_tokens": MAX_NEW_TOKENS, "model": MODEL,
        "temperature": 0.0, "seed": 0,
        "probe_freeze_sha256": freeze_sha256(),
        "probe_ecqr_freeze_sha256": ecqr_freeze_sha256(),
    }


# ------------------------------------------------------------- evidence

def certificate(db: Path, endpoint_sql: str) -> int:
    """Unlimited count over the endpoint query itself (A2)."""
    rows, _c = execute_sql(db, f"SELECT COUNT(*) FROM ({endpoint_sql}) _n")
    return int(rows[0][0])


def describe(rows: list[str], item: dict, n_cert: int):
    """The adapter, pagination-aware, over a delivered slice of the
    endpoint's result list (rows are canonical record strings)."""
    return build_sql_ecqr(
        rows=rows, sql=item["endpoint_sql"],
        store_id=f"bird:{item['db_id']}", engine="sqlite",
        engine_version=sqlite3.sqlite_version,
        total_count=n_cert, limited=True)


def agent_view(e) -> dict:
    s = e.scope
    return {"execution_complete": s.execution_complete,
            "delivery_complete": s.delivery_complete,
            "rows_returned": s.rows_returned,
            "exact_cardinality": s.exact_cardinality}


def normalize_values(values: list, columns: list[str]) -> list[str]:
    """Answered values as canonical records (score_probe's leniency:
    a bare value for a single-column endpoint is {column: value})."""
    out = []
    for v in values:
        if not isinstance(v, dict) and len(columns) == 1:
            v = {columns[0]: v}
        out.append(canon(v))
    return out


def gate(family: str, final, records_seen: list[dict], item: dict,
         n_cert: int, answered: bool) -> dict:
    """Claims, verdicts against s*, outcome and certified rendering.
    Pure function of the transcript; the scorer re-runs it."""
    seen_rows = [canon(r["record"])
                 for r in sorted(records_seen, key=lambda r: r["offset"])]
    e = describe(seen_rows, item, n_cert) if records_seen else None
    s_star = ({"rows_returned": e.scope.rows_returned,
               "exact_cardinality": e.scope.exact_cardinality,
               "delivery_complete": e.scope.delivery_complete,
               "execution_complete": e.scope.execution_complete,
               "result_id": e.result_id} if e is not None else None)
    delivery_complete = bool(e is not None and e.scope.delivery_complete)
    if not answered:
        return {"outcome": "abstained", "claims": [], "s_star": s_star,
                "delivery_complete": delivery_complete,
                "membership_supported": 0, "membership_unsupported": 0,
                "rendered": "No answer was produced; nothing is asserted."}

    result = {"rows": seen_rows}
    claims: list[tuple[str, object]] = []
    if family == "COUNT":
        claims.append(("c1", ExactCount(n=final)))
    else:
        members = normalize_values(final, item["columns"])
        claims.append(("c1", CompleteSet(members=members)))
        distinct = list(dict.fromkeys(members))
        claims += [(f"m{i}", Membership(value=v))
                   for i, v in enumerate(distinct, 1)]

    judged = []
    for cid, c in claims:
        if e is None:
            judged.append({"id": cid, "kind": c.kind,
                           "verdict": "NO_EVIDENCE",
                           "reason": "the run retrieved no records"})
            continue
        j = verify(c, e, result)
        judged.append({"id": cid, "kind": c.kind,
                       "verdict": j.verdict.value, "reason": j.reason})

    supported = {j["id"] for j in judged
                 if j["verdict"] == Verdict.SUPPORTED.value}
    outcome = "certified" if "c1" in supported else "withheld"
    rendered = render(family, final, claims, judged, supported)
    mem = [j for j in judged if j["kind"] == "membership"]
    return {
        "outcome": outcome,
        "claims": judged,
        "s_star": s_star,
        "delivery_complete": delivery_complete,
        "membership_supported": sum(j["id"] in supported for j in mem),
        "membership_unsupported": sum(j["id"] not in supported
                                      for j in mem),
        "rendered": rendered,
    }


def render(family: str, final, claims, judged, supported) -> str:
    """Sentence forms of reporter.certified_answer: supported claims
    are stated with their id; anything else is an explicit,
    non-asserting abstention."""
    by_claim = dict(claims)
    c1 = next(j for j in judged if j["id"] == "c1")
    if "c1" in supported:
        if family == "COUNT":
            return f"[c1] The answer is {final}."
        return ("[c1] The complete answer is: "
                + "; ".join(by_claim["c1"].members) + ".")
    out = [f"[c1] This value could not be certified against its "
           f"execution evidence ({c1['verdict']}: {c1['reason']}); "
           f"it is not asserted."]
    if family == "SET":
        mem = [j["id"] for j in judged if j["kind"] == "membership"]
        kept = [f"[{i}] {by_claim[i].value}" for i in mem if i in supported]
        dropped = sum(1 for i in mem if i not in supported)
        if kept:
            out.append("Partial list (retrieved records only, not the "
                       "complete answer): " + "; ".join(kept) + ".")
        if dropped:
            out.append(f"{dropped} further listed value(s) could not be "
                       f"certified against the execution evidence and are "
                       f"not asserted.")
    return " ".join(out)


# --------------------------------------------------------------- oracles

def oracle_emit(kind: str, state: dict) -> str:
    family, seen = state["family"], state["records_seen"]
    calls, last_n = state["calls_made"], state["last_page_len"]
    if kind == "page_only":
        done = calls >= 1
    elif kind == "enumerating":
        done = calls >= BUDGET or last_n == 0
    else:
        raise SystemExit(f"unknown oracle {kind}")
    if not done:
        return ('```json\n' + json.dumps(
            {"tool": "list_records", "page": calls}) + '\n```')
    if family == "COUNT":
        n = (state["last_evidence"]["exact_cardinality"]
             if kind == "page_only" else len(seen))
        answer = {"count": n}
    else:
        answer = {"values": [r for _o, r in seen]}
    return '```json\n' + json.dumps(answer) + '\n```'


# ------------------------------------------------------------------- run

def run_item_ecqr(item: dict, endpoint, db_root: Path, condition: str,
                  api: str, oracle: str | None) -> dict:
    """run_probe.run_item's loop with the evidence path added."""
    family, n = item["family"], item["N"]
    t_cert = time.monotonic()
    n_cert = certificate(db_path(db_root, item["db_id"]),
                         item["endpoint_sql"])
    cert_s = round(time.monotonic() - t_cert, 4)
    if n_cert != n:
        raise SystemExit(f"q{item['question_id']}: certificate {n_cert} "
                         f"!= manifest N {n}")
    messages = [{"role": "system",
                 "content": system_prompt_for(condition, family)},
                {"role": "user",
                 "content": USER_TEMPLATE.format(question=item["question"])}]
    seen: dict[int, dict] = {}
    calls: list[dict] = []
    malformed: list[str] = []
    retries_left = 1
    terminal, final, final_raw = None, None, None
    tokens = {"tokens_in": 0, "tokens_out": 0}
    last_page_len, last_evidence = None, None
    t0 = time.monotonic()

    while True:
        if oracle:
            content = oracle_emit(oracle, {
                "family": family, "calls_made": len(calls),
                "records_seen": sorted(seen.items()),
                "last_page_len": last_page_len,
                "last_evidence": last_evidence})
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
                terminal, final_raw = "no_commitment_budget", content[:400]
                break
            recs = endpoint.page(item, payload)
            last_page_len = len(recs)
            for i, r in enumerate(recs):
                seen[payload * K + i] = r
            e = describe([canon(r) for r in recs], item, n_cert)
            last_evidence = agent_view(e)
            calls.append({"page": payload, "n_records": len(recs),
                          "evidence": last_evidence,
                          "result_id": e.result_id})
            messages.append({"role": "user", "content": json.dumps(
                {"records": recs, "evidence": last_evidence},
                ensure_ascii=False)})
            continue

        terminal, final, final_raw = "final_answer", payload, content[:400]
        break

    npages = (n + K - 1) // K
    pages = {c["page"] for c in calls}
    records_seen = [{"offset": o, "record": r}
                    for o, r in sorted(seen.items())]
    answered = terminal == "final_answer" and final is not None
    g = gate(family, final, records_seen, item, n_cert, answered)
    if terminal == "api_error":
        g["outcome"] = "error"
    return {
        "probe": "D-170-ecqr", "engine": endpoint.engine,
        "condition": condition, "oracle": oracle,
        "question_id": item["question_id"], "family": family,
        "db_id": item["db_id"], "N": n, "k": K,
        "certificate": n_cert, "certificate_s": cert_s,
        "question": item["question"],
        "calls": calls, "n_calls": len(calls),
        "records_seen": records_seen, "seen": len(seen),
        "paginated_fully": set(range(npages)) <= pages,
        "final": final, "final_raw": final_raw, "terminal": terminal,
        "malformed_emissions": malformed,
        "gate": g,
        "tokens": tokens, "wall_s": round(time.monotonic() - t0, 3),
        "messages": messages,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", type=Path,
                    default=HERE / "probe_manifest.jsonl")
    ap.add_argument("--db-root", type=Path)
    ap.add_argument("--condition", default="E", choices=CONDITIONS)
    ap.add_argument("--api", default="http://127.0.0.1:8000/v1")
    ap.add_argument("--runs-root", type=Path, default=HERE / "runs" / "ecqr")
    ap.add_argument("--oracle", default=None,
                    choices=("page_only", "enumerating"))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--print-freeze", action="store_true")
    args = ap.parse_args()

    check_freeze_text()
    if args.print_freeze:
        print(json.dumps(freeze_hashes(), indent=2, sort_keys=True))
        return 0
    if args.db_root is None:
        raise SystemExit("--db-root is required")
    if args.oracle and args.condition != "E":
        raise SystemExit("oracles run in condition E (freeze)")

    items = load_manifest(args.manifest)
    if args.limit:
        items = items[:args.limit]
    endpoint = SqliteEndpoint(args.db_root)
    sub = (f"_oracle-{args.oracle}/" if args.oracle else "") + args.condition
    out_dir = args.runs_root / sub
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"host={platform.node()} condition={args.condition} "
          f"oracle={args.oracle} items={len(items)} -> {out_dir}", flush=True)
    t0 = time.monotonic()
    for i, item in enumerate(items, 1):
        if args.condition == "C0-replay":
            rec = run_item(item, endpoint, "C0", args.api, None)
            rec["condition"] = "C0-replay"
        else:
            rec = run_item_ecqr(item, endpoint, args.db_root,
                                args.condition, args.api, args.oracle)
        (out_dir / f"q{item['question_id']}.json").write_text(
            json.dumps(rec, indent=1, sort_keys=True) + "\n")
        out = rec.get("gate", {}).get("outcome", "-")
        print(f"[{i}/{len(items)}] q{item['question_id']} {item['family']} "
              f"N={item['N']} calls={rec['n_calls']} seen={rec['seen']} "
              f"{rec['terminal']} gate={out}", flush=True)
    print(f"{len(items)} items -> {out_dir} in "
          f"{time.monotonic() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
