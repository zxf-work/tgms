#!/usr/bin/env python3
"""Independent-question study (CIDR review §10): classification, gold, suite.

110 questions were written by students who saw only a natural-language
dataset description (benchmarks/independent-v1/raw_questions.txt) — never
the operator list, the repo, or existing benchmark tasks.

Each question is classified against the fixed 13-operator algebra:
  1 directly expressible (single operator + answer readout)
  2 expressible by operator composition
  3 requires an unimplemented capability (see `need` tags)
  4 ambiguous / mistaken presupposition as written
  5 not a computation-over-the-log question
`need` tags: G grouped/distinct aggregation over accounts or pairs;
CAL calendar semantics (weekday, hour-of-day, same-calendar-unit);
AR arithmetic beyond count/sum/min/max/topk (diff, ratio, avg, median, %);
SET set operations over uid sets; PROP property-filtered pattern operators
(rating sign in motifs/reachability); GLOB global event/version scan-select
(argmax over all events, correction records); NEG absence conditions.

Class 1-2 questions with a scoreable answer kind and no compound answer
run verbatim (run=True); gold is computed two independent ways (SQL over
the canonical DuckDB store; pure Python over the event rows) and must
agree.

Usage (on the host that has the canonical stores):
  python3 scripts/independent_questions.py report
  python3 scripts/independent_questions.py build   # writes suites + gold
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

MICROS_PER_DAY = 86_400_000_000
OPEN_END = 2**62
RAW = Path("benchmarks/independent-v1/raw_questions.txt")

# --------------------------------------------------------------------------- #
# classification (hand-audited against the operator registry, 2026-07-26)      #
# --------------------------------------------------------------------------- #
# value = (class, need/ops, run, note)
C: dict[tuple[str, int], tuple[int, str, bool, str]] = {
    # ---- CollegeMsg (extent 2004-04-15 .. 2004-10-26) ----
    ("cm", 1): (3, "G", False, "distinct senders in window"),
    ("cm", 2): (3, "G", False, ""),
    ("cm", 3): (3, "G", False, "empty month (data start Apr 15)"),
    ("cm", 4): (3, "G", False, "partial window vs data extent"),
    ("cm", 5): (2, "graph_metric_timeseries+topk", True, "daily buckets"),
    ("cm", 6): (3, "GLOB", False, "global longest cascade"),
    ("cm", 7): (3, "G", False, "empty week (Jan)"),
    ("cm", 8): (2, "entity_history+filter+min", False,
                "compound answer (who + date) not scoreable"),
    ("cm", 9): (3, "G", False, "per-account sliding window"),
    ("cm", 10): (3, "G,SET", False, ""),
    ("cm", 11): (3, "AR,G", False, "average"),
    ("cm", 12): (2, "entity_history+filter", True, "incoming on one day"),
    ("cm", 13): (3, "G", False, "distinct pairs; near count_temporal_motifs"),
    ("cm", 14): (3, "G", False, "argmax gap per account"),
    ("cm", 15): (3, "CAL,AR", False, "hour-of-day grouping, %"),
    ("cm", 16): (3, "G", False, "per-contact first-in/first-out compare"),
    ("cm", 17): (3, "G", False, ""),
    ("cm", 18): (2, "graph_metric_timeseries+sum", True, "first-week burst"),
    ("cm", 19): (3, "CAL", False, "same-calendar-day constraint; near CTM"),
    ("cm", 20): (3, "CAL,G", False, "weekday grouping"),
    ("cm", 21): (3, "G,CAL", False, ""),
    ("cm", 22): (3, "AR,G", False, "median"),
    ("cm", 23): (3, "G", False, "top-3 distinct recipients"),
    ("cm", 24): (3, "G", False, "per-account gap pattern"),
    ("cm", 25): (3, "GLOB", False, "src==dst predicate unsupported"),
    ("cm", 26): (2, "entity_history+min+interval_relation", False,
                 "categorical answer (sent-first/received-first)"),
    ("cm", 27): (3, "AR", False, "difference of two counts"),
    ("cm", 28): (3, "G,SET,NEG", False, ""),
    ("cm", 29): (3, "AR,G", False, "ratio"),
    ("cm", 30): (3, "G,CAL", False, "empty months Jan-Mar"),
    ("cm", 31): (3, "G", False, "per-account 1h window argmax"),
    ("cm", 32): (3, "CAL", False, "weekday + hour-of-day"),
    ("cm", 33): (3, "G,AR", False, "median over top-10 group"),
    ("cm", 34): (3, "G,AR", False, ""),
    ("cm", 35): (3, "G", False, "per-pair sessionization"),
    ("cm", 36): (3, "SET", False, "uid-set difference; near entity_history"),
    ("cm", 37): (3, "GLOB", False, "global argmax event; motif rows nested"),
    ("cm", 38): (3, "GLOB", False, "duplicate of Q25"),
    ("cm", 39): (3, "G,CAL", False, ""),
    ("cm", 40): (3, "G,NEG", False, ""),
    ("cm", 41): (3, "G,AR,CAL", False, "weekly new-contact average"),
    ("cm", 42): (3, "SET,CAL", False, "join on (dst, same-day)"),
    ("cm", 43): (3, "AR,CAL", False, "compare two buckets; Feb empty"),
    ("cm", 44): (3, "G,AR", False, "average reply time argmax"),
    ("cm", 45): (3, "G,SET", False, "Jan empty -> degenerate"),
    ("cm", 46): (2, "graph_metric_timeseries+filter", False,
                 "yes/no answer kind not scoreable"),
    ("cm", 47): (3, "G", False, "second-highest rank"),
    ("cm", 48): (3, "G,CAL", False, ""),
    ("cm", 49): (3, "G", False, "per-recipient join of two accounts"),
    ("cm", 50): (3, "G", False, "pairs with exactly one message"),
    ("cm", 51): (3, "G", False, ""),
    ("cm", 52): (3, "G,AR", False, "ratio; March empty"),
    ("cm", 53): (3, "G,CAL", False, ""),
    ("cm", 54): (3, "G,AR", False, "top-1% share"),
    ("cm", 55): (2, "entity_history+max+interval_relation", False,
                 "categorical answer (before/after)"),
    # ---- Bitcoin-OTC (extent 2010-11-08 .. 2016-01-25) ----
    ("bo", 1): (3, "G", False, ""),
    ("bo", 2): (3, "G", False, ""),
    ("bo", 3): (2, "graph_metric_timeseries+sum", True, "total rating events"),
    ("bo", 4): (3, "GLOB,PROP", False, "global selection by rating value"),
    ("bo", 5): (2, "graph_metric_timeseries+topk", True, "daily buckets"),
    ("bo", 6): (3, "AR,PROP", False, "percentage"),
    ("bo", 7): (3, "G,SET", False, ""),
    ("bo", 8): (3, "AR,CAL,G", False, "yearly averages"),
    ("bo", 9): (3, "G", False, ""),
    ("bo", 10): (3, "G", False, ""),
    ("bo", 11): (3, "G,AR", False, ""),
    ("bo", 12): (3, "G,AR", False, ""),
    ("bo", 13): (3, "GLOB,PROP", False, "global min over rating property"),
    ("bo", 14): (3, "G,AR,PROP", False, ""),
    ("bo", 15): (3, "G,PROP", False, ""),
    ("bo", 16): (3, "G", False, "per-account first rating"),
    ("bo", 17): (3, "G", False, ""),
    ("bo", 18): (3, "G", False, "any-account burst; burst_detection is per-uid"),
    ("bo", 19): (3, "G,CAL", False, ""),
    ("bo", 20): (3, "G,AR", False, "median"),
    ("bo", 21): (2, "graph_metric_timeseries+filter", False,
                 "yes/no answer kind not scoreable"),
    ("bo", 22): (3, "G,CAL", False, ""),
    ("bo", 23): (3, "G,AR", False, ""),
    ("bo", 24): (3, "G", False, "distinct reciprocal pairs; near CTM"),
    ("bo", 25): (3, "AR,PROP,G", False, ""),
    ("bo", 26): (3, "G,PROP", False, ""),
    ("bo", 27): (3, "G", False, "re-rating detection per pair"),
    ("bo", 28): (3, "G", False, ""),
    ("bo", 29): (3, "G,PROP", False, ""),
    ("bo", 30): (3, "G,PROP", False, ""),
    ("bo", 31): (3, "NEG,PROP,G", False, "absent-edge condition"),
    ("bo", 32): (3, "PROP,CAL", False, "sign filter in path motif"),
    ("bo", 33): (3, "G,PROP", False, ""),
    ("bo", 34): (3, "PROP,GLOB", False, ""),
    ("bo", 35): (3, "PROP,G", False, ""),
    ("bo", 36): (3, "PROP", False, "signed directed 2-hop; snapshot is "
                                   "undirected, unsigned"),
    ("bo", 37): (3, "PROP", False, "sign filter in cyclic motif"),
    ("bo", 38): (4, "", False, "clock conflation: presupposes the database "
                 "held beliefs on 2013-01-01; transaction time begins at the "
                 "2026 ingest, so the belief state at that tt is empty"),
    ("bo", 39): (5, "", False, "presupposes a specific correction (n77, "
                 "2012-03-15) that does not exist in the data"),
    ("bo", 40): (3, "GLOB", False, "global version/correction scan"),
    ("bo", 41): (3, "GLOB,CAL", False, ""),
    ("bo", 42): (3, "G,AR", False, "also clock conflation (2012 tt)"),
    ("bo", 43): (3, "G,AR", False, "also clock conflation (2011 tt)"),
    ("bo", 44): (3, "G", False, "also clock conflation"),
    ("bo", 45): (3, "GLOB", False, "event-time vs record-time comparison"),
    ("bo", 46): (3, "G,PROP,AR", False, ""),
    ("bo", 47): (3, "G,AR", False, ""),
    ("bo", 48): (3, "G,PROP", False, ""),
    ("bo", 49): (3, "G,PROP", False, ""),
    ("bo", 50): (3, "G,PROP,AR", False, ""),
    ("bo", 51): (3, "G,CAL", False, ""),
    ("bo", 52): (3, "G", False, "depends on top-10 group-by"),
    ("bo", 53): (3, "G,SET", False, ""),
    ("bo", 54): (3, "G,AR", False, ""),
    ("bo", 55): (3, "AR", False, "near-miss: sum and count expressible for "
                                 "the named account, but no division"),
}


def parse_raw() -> dict[tuple[str, int], dict]:
    """Parse raw_questions.txt into {(dataset, n): {text, kind, why}}."""
    out: dict[tuple[str, int], dict] = {}
    ds = "cm"
    seen_blank = False
    for line in RAW.read_text().splitlines():
        line = line.strip()
        if not line:
            seen_blank = True
            continue
        if "|" not in line:
            if seen_blank:
                ds = "bo"  # section headers after the gap = bitcoin block
            continue
        qid, text, kind, why = (p.strip() for p in line.split("|", 3))
        n = int(qid.lstrip("Q"))
        out[(ds, n)] = {"text": text, "kind": kind, "why": why}
    return out


def utc(y, m, d):
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp() * 1_000_000)


# --------------------------------------------------------------------------- #
# gold: two independent keys per runnable question                             #
# --------------------------------------------------------------------------- #

def _events_sql(db):  # key 1: SQL over the canonical store (current belief)
    import duckdb
    conn = duckdb.connect(db, read_only=True)
    return conn


def _events_py(db):   # key 2: pure Python row list
    import duckdb
    conn = duckdb.connect(db, read_only=True)
    rows = conn.execute(
        "SELECT src, dst, vt_s FROM edge_versions "
        f"WHERE tt_e = {OPEN_END}").fetchall()
    conn.close()
    return rows


def gold_busiest_day(db):
    conn = _events_sql(db)
    day, cnt = conn.execute(
        f"SELECT vt_s // {MICROS_PER_DAY} AS d, count(*) c FROM edge_versions "
        f"WHERE tt_e = {OPEN_END} GROUP BY d ORDER BY c DESC, d LIMIT 1"
    ).fetchone()
    conn.close()
    py = Counter(v // MICROS_PER_DAY for _, _, v in _events_py(db))
    top = max(sorted(py), key=lambda d: py[d])
    assert top == day and py[top] == cnt, (top, day)
    return {"t_a": day * MICROS_PER_DAY, "t_b": (day + 1) * MICROS_PER_DAY}


def gold_senders_to(db, uid, t_a, t_b):
    conn = _events_sql(db)
    rows = conn.execute(
        "SELECT DISTINCT src FROM edge_versions WHERE dst = ? "
        f"AND vt_s >= ? AND vt_s < ? AND tt_e = {OPEN_END}",
        [uid, t_a, t_b]).fetchall()
    conn.close()
    py = sorted({s for s, d, v in _events_py(db)
                 if d == uid and t_a <= v < t_b})
    sql = sorted(r[0] for r in rows)
    assert sql == py, (sql, py)
    return sql


def gold_count_window(db, t_a, t_b):
    conn = _events_sql(db)
    (n,) = conn.execute(
        "SELECT count(*) FROM edge_versions WHERE vt_s >= ? AND vt_s < ? "
        f"AND tt_e = {OPEN_END}", [t_a, t_b]).fetchone()
    conn.close()
    n2 = sum(1 for _, _, v in _events_py(db) if t_a <= v < t_b)
    assert n == n2
    return n


def build():
    import os
    q = parse_raw()
    # env overrides let gold run on snapshot copies when the live store's
    # write lock is held by a running eval
    cm_db = os.environ.get("TGMS_CM_DB", "stores/collegemsg/store.duckdb")
    bo_db = os.environ.get("TGMS_BO_DB", "stores/bitcoinotc/store.duckdb")
    conn = _events_sql(cm_db)
    (cm_min,) = conn.execute(
        f"SELECT min(vt_s) FROM edge_versions WHERE tt_e = {OPEN_END}"
    ).fetchone()
    conn.close()

    tasks = {
        "cm": [
            dict(id="indep-cm-05", n=5, answer_kind="interval",
                 gold=gold_busiest_day(cm_db), input_uids=[]),
            dict(id="indep-cm-12", n=12, answer_kind="entity_set",
                 gold=gold_senders_to(cm_db, "n5",
                                      utc(2004, 6, 15), utc(2004, 6, 16)),
                 input_uids=["n5"]),
            dict(id="indep-cm-18", n=18, answer_kind="count",
                 gold=gold_count_window(cm_db, cm_min,
                                        cm_min + 7 * MICROS_PER_DAY),
                 input_uids=[]),
        ],
        "bo": [
            dict(id="indep-bo-03", n=3, answer_kind="count",
                 gold=gold_count_window(bo_db, 0, OPEN_END), input_uids=[]),
            dict(id="indep-bo-05", n=5, answer_kind="interval",
                 gold=gold_busiest_day(bo_db), input_uids=[]),
        ],
    }
    names = {"cm": ("collegemsg", "suite-indep-collegemsg"),
             "bo": ("bitcoinotc", "suite-indep-bitcoinotc")}
    for ds, (dataset, out) in names.items():
        suite = {"dataset": dataset, "seed": 0, "dev": [],
                 "test": [{**t, "dataset": dataset, "family": "independent",
                           "difficulty": "independent", "gold_source": "manual-double-keyed",
                           "question_text": q[(ds, t.pop("n"))]["text"]}
                          for t in tasks[ds]],
                 "n_dev": 0, "n_test": len(tasks[ds])}
        import hashlib
        suite["test_split_sha"] = hashlib.sha256(
            json.dumps(suite["test"], sort_keys=True).encode()).hexdigest()
        p = Path("stores") / out
        p.mkdir(parents=True, exist_ok=True)
        (p / "suite.json").write_text(json.dumps(suite, indent=1))
        print(out, suite["n_test"], "tasks", suite["test_split_sha"][:16])


def report():
    q = parse_raw()
    assert len(q) == 110, len(q)
    dist = Counter(C[k][0] for k in q)
    print("class distribution:", dict(sorted(dist.items())))
    need = Counter(tag for k in q for tag in C[k][1].split(",")
                   if C[k][0] == 3 and tag and "+" not in tag)
    print("missing capabilities (class 3):", dict(need.most_common()))
    print("runnable:", [f"{d}-Q{n}" for (d, n) in sorted(C)
                        if C[(d, n)][2]])
    rows = [{"dataset": d, "q": n, **q[(d, n)],
             "class": C[(d, n)][0], "need_or_ops": C[(d, n)][1],
             "run": C[(d, n)][2], "note": C[(d, n)][3]}
            for (d, n) in sorted(q)]
    out = Path("benchmarks/independent-v1/classification.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=1))
    print("wrote", out)


if __name__ == "__main__":
    {"report": report, "build": build}[sys.argv[1] if len(sys.argv) > 1
                                       else "report"]()
