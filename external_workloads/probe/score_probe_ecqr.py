#!/usr/bin/env python
"""Truncation probe under ECQR gating (D-170, lane D): SCORER.

Deterministic, from the transcripts under runs/ecqr/ plus the pinned
databases (gold lists of SET questions) and the committed baseline
receipt. PROBE_ECQR_FREEZE.md:

  proposed class  score_probe.classify, unchanged (page_derived,
                  correct, no_commitment, other_wrong, error)
  gate outcome    certified | withheld | abstained | error, recomputed
                  here from the transcript with run_probe_ecqr.gate and
                  required to equal the runner's record

    python external_workloads/probe/score_probe_ecqr.py \
        --db-root .../minidev/MINIDEV/dev_databases \
        --out benchmarks/results-v1/eval-trunc-probe-ecqr.json \
        [--oracle-check page_only|enumerating] [--commit SHA]
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import platform
import sqlite3
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_probe_ecqr import (CONDITIONS, OUTCOMES,  # noqa: E402
                            ecqr_freeze_sha256, freeze_hashes, gate)
from score_probe import classify  # noqa: E402
from setup_probe import (BUDGET, K, freeze_sha256,  # noqa: E402
                         load_manifest)

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
BASELINE = REPO / "benchmarks" / "results-v1" / "eval-trunc-probe.json"
PROPOSED = ("page_derived", "correct", "no_commitment", "other_wrong",
            "error")


def certified_correct(rec: dict, cls: str) -> bool:
    """A certified answer is correct under the probe's definition."""
    return rec["gate"]["outcome"] == "certified" and cls == "correct"


def score_item(rec: dict, item: dict, db_root: Path) -> dict:
    row = classify(rec, item, db_root)
    out = {"question_id": rec["question_id"], "family": rec["family"],
           "N": rec["N"], "n_calls": rec["n_calls"], "seen": rec["seen"],
           "terminal": rec["terminal"], "proposed": row["class"],
           "tokens_in": rec["tokens"]["tokens_in"],
           "tokens_out": rec["tokens"]["tokens_out"]}
    if "gate" not in rec:              # C0-replay: no evidence path
        return out
    answered = rec["terminal"] == "final_answer" and rec["final"] is not None
    g = gate(rec["family"], rec["final"], rec["records_seen"], item,
             rec["certificate"], answered)
    if rec["terminal"] == "api_error":
        g["outcome"] = "error"
    if g != rec["gate"]:
        raise SystemExit(f"q{rec['question_id']} ({rec['condition']}): "
                         f"recomputed gate differs from the runner's")
    c1 = next((c for c in g["claims"] if c["id"] == "c1"), None)
    out.update({
        "outcome": g["outcome"],
        "c1_verdict": c1["verdict"] if c1 else None,
        "delivery_complete": g["delivery_complete"],
        "membership_supported": g["membership_supported"],
        "membership_unsupported": g["membership_unsupported"],
        "certified_correct": certified_correct(rec, row["class"]),
        "user_visible_wrong": (g["outcome"] == "certified"
                               and row["class"] != "correct"),
    })
    return out


def _frac(a: int, n: int):
    return round(a / n, 6) if n else None


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    out: dict = {"n": n}
    out["proposed"] = {c: sum(r["proposed"] == c for r in rows)
                       for c in PROPOSED}
    calls = [r["n_calls"] for r in rows]
    out["calls_per_question"] = {
        "mean": round(statistics.mean(calls), 4) if calls else None,
        "median": statistics.median(calls) if calls else None,
        "total": sum(calls)}
    out["tokens_in_total"] = sum(r["tokens_in"] for r in rows)
    out["tokens_out_total"] = sum(r["tokens_out"] for r in rows)
    le100 = [r for r in rows if r["N"] <= K * BUDGET]
    out["n_le_100"] = len(le100)
    if rows and "outcome" in rows[0]:
        out["outcome"] = {o: sum(r["outcome"] == o for r in rows)
                          for o in OUTCOMES}
        out["certified_correct"] = sum(r["certified_correct"] for r in rows)
        out["user_visible_wrong"] = sum(r["user_visible_wrong"]
                                        for r in rows)
        out["user_visible_abstention"] = (out["outcome"]["withheld"]
                                          + out["outcome"]["abstained"])
        out["delivery_complete"] = sum(r["delivery_complete"] for r in rows)
        out["delivery_complete_le_100"] = sum(r["delivery_complete"]
                                              for r in le100)
        out["page_derived_certified"] = sum(
            r["proposed"] == "page_derived" and r["outcome"] == "certified"
            for r in rows)
        out["page_derived_withheld"] = sum(
            r["proposed"] == "page_derived" and r["outcome"] == "withheld"
            for r in rows)
        wh = [r for r in rows if r["outcome"] == "withheld"]
        out["withheld_with_partial_list"] = sum(
            r["membership_supported"] > 0 for r in wh)
        out["membership_supported_total"] = sum(
            r["membership_supported"] for r in rows)
        out["membership_unsupported_total"] = sum(
            r["membership_unsupported"] for r in rows)
        out["percent"] = {
            "page_derived": _frac(out["proposed"]["page_derived"], n),
            "correct": _frac(out["proposed"]["correct"], n),
            "certified": _frac(out["outcome"]["certified"], n),
            "certified_correct": _frac(out["certified_correct"], n),
            "withheld": _frac(out["outcome"]["withheld"], n),
            "abstained": _frac(out["outcome"]["abstained"], n),
            "error": _frac(out["outcome"]["error"], n),
            "user_visible_abstention": _frac(
                out["user_visible_abstention"], n),
            "user_visible_wrong": _frac(out["user_visible_wrong"], n),
            "delivery_complete": _frac(out["delivery_complete"], n),
            "delivery_complete_le_100": _frac(
                out["delivery_complete_le_100"], len(le100)),
        }
    else:
        out["percent"] = {c: _frac(out["proposed"][c], n) for c in PROPOSED}
    return out



def expected(kind: str, item: dict) -> dict:
    """What a scripted oracle MUST score (PROBE_ECQR_FREEZE.md)."""
    if kind == "page_only":
        if item["family"] == "COUNT":
            return {"proposed": "correct", "outcome": "certified",
                    "delivery_complete": False}
        return {"proposed": "page_derived", "outcome": "withheld",
                "delivery_complete": False, "membership_unsupported": 0}
    if item["N"] <= K * BUDGET:
        return {"proposed": "correct", "outcome": "certified",
                "delivery_complete": True}
    return {"proposed": "page_derived", "outcome": "withheld",
            "delivery_complete": False}


def oracle_mismatches(kind: str, rows: list[dict], by_qid: dict) -> list:
    bad = []
    for r in rows:
        want = expected(kind, by_qid[r["question_id"]])
        got = {k: r[k] for k in want}
        ok = got == want
        if kind == "page_only" and r["family"] == "SET":
            # every listed record of page 0 is witnessed
            ok = ok and r["membership_supported"] > 0
        if not ok:
            bad.append({"question_id": r["question_id"],
                        "family": r["family"], "N": r["N"],
                        "got": {**got, "membership_supported":
                                r["membership_supported"]},
                        "want": want})
    return bad


def score_root(root: Path, by_qid: dict, db_root: Path):
    conditions, per_item = {}, {}
    for cond in CONDITIONS:
        d = root / cond
        if not d.is_dir():
            continue
        rows = []
        for f in sorted(d.glob("q*.json"), key=lambda p: int(p.stem[1:])):
            rec = json.loads(f.read_text())
            rows.append(score_item(rec, by_qid[rec["question_id"]],
                                   db_root))
        if not rows:
            continue
        conditions[cond] = {
            "all": summarize(rows),
            "by_family": {fam: summarize([r for r in rows
                                          if r["family"] == fam])
                          for fam in ("COUNT", "SET")},
        }
        per_item[cond] = rows
    return conditions, per_item


def oracle_result(kind: str, per_item: dict, by_qid: dict) -> dict:
    mism = {c: oracle_mismatches(kind, rows, by_qid)
            for c, rows in per_item.items()}
    n = sum(len(r) for r in per_item.values())
    return {"oracle": kind, "items_checked": n, "mismatches": mism,
            "passed": n == len(by_qid) and not any(mism.values())}


def headline(base: dict, conditions: dict) -> list[dict]:
    """Condition x family rows. Baselines have no gate: every committed
    answer is shown, so certified/withheld do not apply and abstained
    is the agent's own no_commitment. Baseline calls are C0-replay's
    (C0 only; the committed C1/C2 transcripts carry no call counts)."""
    rows = []
    for cond in ("C0", "C1", "C2"):
        for fam in ("COUNT", "SET", "all"):
            b = base[cond][fam]
            calls = None
            if cond == "C0" and "C0-replay" in conditions:
                c = conditions["C0-replay"]
                s = c["all"] if fam == "all" else c["by_family"][fam]
                calls = s["calls_per_question"]
            rows.append({
                "condition": cond, "family": fam, "n": b["n"],
                "page_derived_pct": b["percent"]["page_derived"],
                "correct_pct": b["percent"]["correct"],
                "certified_pct": None, "withheld_pct": None,
                "abstained_pct": b["percent"]["no_commitment"],
                "user_visible_wrong_pct": b["percent"]["user_visible_wrong"],
                "calls_per_question": calls,
                "calls_source": "C0-replay" if calls else None})
    for cond in ("E", "E-aware"):
        if cond not in conditions:
            continue
        c = conditions[cond]
        for fam in ("COUNT", "SET", "all"):
            s = c["all"] if fam == "all" else c["by_family"][fam]
            p = s["percent"]
            rows.append({
                "condition": cond, "family": fam, "n": s["n"],
                "page_derived_pct": p["page_derived"],
                "correct_pct": p["correct"],
                "certified_pct": p["certified"],
                "certified_correct_pct": p["certified_correct"],
                "withheld_pct": p["withheld"],
                "abstained_pct": p["abstained"],
                "error_pct": p["error"],
                "user_visible_abstention_pct": p["user_visible_abstention"],
                "user_visible_wrong_pct": p["user_visible_wrong"],
                "delivery_complete": s["delivery_complete"],
                "delivery_complete_le_100": s["delivery_complete_le_100"],
                "n_le_100": s["n_le_100"],
                "calls_per_question": s["calls_per_question"],
                "calls_source": cond})
    return rows


def file_sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def baseline_rows() -> dict:
    b = json.loads(BASELINE.read_text())
    out = {}
    for cond in ("C0", "C1", "C2"):
        c = b["conditions"][cond]
        out[cond] = {}
        for fam, s in [("all", c["all"])] + sorted(c["by_family"].items()):
            n = s["n"]
            committed = n - s["no_commitment"] - s["error"]
            out[cond][fam] = {
                "n": n,
                "page_derived": s["page_derived"], "correct": s["correct"],
                "no_commitment": s["no_commitment"],
                "other_wrong": s["other_wrong"], "error": s["error"],
                "paginated_fully": s["paginated_fully"],
                "n_le_100": s["n_le_100"],
                "percent": {
                    "page_derived": _frac(s["page_derived"], n),
                    "correct": _frac(s["correct"], n),
                    "no_commitment": _frac(s["no_commitment"], n),
                    # every committed answer is user-visible
                    "user_visible_wrong": _frac(committed - s["correct"], n),
                },
            }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", type=Path,
                    default=HERE / "probe_manifest.jsonl")
    ap.add_argument("--db-root", type=Path, required=True)
    ap.add_argument("--runs-root", type=Path, default=HERE / "runs" / "ecqr")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--oracle-check", default=None,
                    choices=("page_only", "enumerating"))
    ap.add_argument("--commit", default="unknown")
    ap.add_argument("--node", default=platform.node())
    ap.add_argument("--slurm-job", default=None)
    args = ap.parse_args()

    by_qid = {i["question_id"]: i for i in load_manifest(args.manifest)}
    root = (args.runs_root / f"_oracle-{args.oracle_check}"
            if args.oracle_check else args.runs_root)
    conditions, per_item = score_root(root, by_qid, args.db_root)

    receipt: dict = {
        "probe": "truncation probe under ECQR gating (D-170, lane D)",
        "date_utc": datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="seconds"),
        "commit": args.commit, "host": args.node,
        "slurm_job": args.slurm_job,
        "sqlite_version": sqlite3.sqlite_version,
        "python": platform.python_version(),
        "probe_ecqr_freeze_sha256": ecqr_freeze_sha256(),
        "probe_freeze_sha256": freeze_sha256(),
        "protocol": freeze_hashes(),
        "manifest": str(args.manifest.relative_to(REPO))
        if args.manifest.is_relative_to(REPO) else str(args.manifest),
        "manifest_sha256": file_sha256(args.manifest),
        "eligible": len(by_qid),
        "conditions": conditions,
        "per_item": per_item,
    }
    if not args.oracle_check:
        receipt["baseline"] = {
            "source": str(BASELINE.relative_to(REPO)),
            "sha256": file_sha256(BASELINE),
            "rows": baseline_rows(),
        }
        if "C0-replay" in per_item:
            committed = json.loads(BASELINE.read_text())["per_item_class"]["C0"]
            agree = [r for r in per_item["C0-replay"]
                     if committed.get(str(r["question_id"])) == r["proposed"]]
            receipt["c0_replay_reproducibility"] = {
                "items": len(per_item["C0-replay"]),
                "class_agrees_with_committed_C0": len(agree),
                "disagreements": [
                    {"question_id": r["question_id"],
                     "committed": committed.get(str(r["question_id"])),
                     "replay": r["proposed"]}
                    for r in per_item["C0-replay"] if r not in agree],
            }
        receipt["headline"] = headline(receipt["baseline"]["rows"],
                                       conditions)
        # the oracle validation of this same harness, rescored here
        receipt["oracle_validation"] = {}
        for kind in ("page_only", "enumerating"):
            od = args.runs_root / f"_oracle-{kind}"
            if od.is_dir():
                _c, pi = score_root(od, by_qid, args.db_root)
                receipt["oracle_validation"][kind] = oracle_result(
                    kind, pi, by_qid)
    else:
        receipt["oracle_check"] = oracle_result(args.oracle_check,
                                                per_item, by_qid)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(receipt, indent=1, sort_keys=True) + "\n")

    for cond, c in conditions.items():
        for fam in ("COUNT", "SET"):
            s = c["by_family"][fam]
            p = s["percent"]
            line = (f"{cond:9s} {fam:5s} n={s['n']:3d} "
                    f"page_derived={s['proposed']['page_derived']:3d} "
                    f"correct={s['proposed']['correct']:3d} "
                    f"calls_mean={s['calls_per_question']['mean']}")
            if "outcome" in s:
                line += (f" certified={s['outcome']['certified']:3d} "
                         f"withheld={s['outcome']['withheld']:3d} "
                         f"abstained={s['outcome']['abstained']:3d} "
                         f"error={s['outcome']['error']:3d} "
                         f"wrong_visible={s['user_visible_wrong']} "
                         f"complete={s['delivery_complete']}"
                         f" ({p['certified']})")
            print(line)
    if args.oracle_check:
        oc = receipt["oracle_check"]
        if not oc["passed"]:
            print(f"ORACLE CHECK FAILED ({args.oracle_check})")
            print(json.dumps(oc["mismatches"], indent=1)[:3000])
            return 1
        print(f"ORACLE CHECK PASSED: {oc['items_checked']} items "
              f"({args.oracle_check})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
